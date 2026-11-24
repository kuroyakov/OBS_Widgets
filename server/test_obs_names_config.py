#!/usr/bin/env python3
"""
Issue #25 (PR 1): /api/config の OBS 名前設定配信・パーサー・env-config.js 反映のテスト
- server.config.read_env_local_obs_names の安全な JS パース (日本語/括弧/クォート/コメント/末尾カンマ/ファイル未存在/キャッシュ)
- GET /api/config の経路別アクセス制御 (localhost / LAN 未認証・不正トークン / LAN 有効トークン / Origin: null)
- LAN からの /shared/env-config.local.js GET/HEAD が引き続き 403 であること
- shared/env-config.js のインプレース反映・localEnv 優先・configStatus (ok / unauthorized / error)
"""

import http.client
import json
import os
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


def _raw_http_request(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        req_headers = dict(headers or {})
        conn.putrequest(method, path)
        for k, v in req_headers.items():
            conn.putheader(k, v)
        if body is not None and "Content-Length" not in {k.title() for k in req_headers}:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or b"")
        resp = conn.getresponse()
        raw_body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw_body
    finally:
        conn.close()


class TestEnvLocalObsNamesParser(unittest.TestCase):
    def test_missing_file_returns_empty_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "env-config.local.js")
            cache = {"key": None, "value": None}
            res = server_config.read_env_local_obs_names(env_file=env_path, cache=cache)
            self.assertEqual(
                res,
                {
                    "obs_scene_names": {},
                    "obs_media_sources": {},
                    "obs_filter_names": {},
                    "obs_audio_sources": {},
                },
            )

    def test_parse_japanese_brackets_quotes_comments_trailing_commas(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "env-config.local.js")
            cache = {"key": None, "value": None}
            js_content = """\
/**
 * テスト用ローカル設定 (コメント内の OBS_SCENE_NAMES: { starting: '無視' } は読まない)
 */
window.OBS_LOCAL_ENV = {
  OBS_DEFAULT_PASSWORD: 'dummy-password',
  // 行コメント内の OBS_AUDIO_SOURCES: { desktop: '無視' },
  OBS_SCENE_NAMES: {
    starting: '開始画面（待機）', // 日本語 + 全角括弧
    "liveMain": "メイン [配信用] // スラッシュ入り",
    'liveChat': '雑談 (コメント枠)',
    ending: '終了\\'画面\\\\テスト', // エスケープ + 末尾カンマ
  },
  OBS_MEDIA_SOURCES: {
    intro: "イントロ(BGM)", /* ブロックコメント */
    bgm: 'BGM【通常】',
    outro: 'アウトロ',
    keyboardVisualizer: 'KeyboardVisualizer',
  },
  OBS_FILTER_NAMES: {
    bgmFadeIn: 'フェードイン (In)',
    bgmFadeOut: "フェードアウト (Out)",
  },
  OBS_AUDIO_SOURCES: {
    desktop: 'デスクトップ音声（メイン）',
  },
  SERVER_PORT: 8080,
};
"""
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(js_content)

            res = server_config.read_env_local_obs_names(env_file=env_path, cache=cache)
            self.assertEqual(
                res["obs_scene_names"],
                {
                    "starting": "開始画面（待機）",
                    "liveMain": "メイン [配信用] // スラッシュ入り",
                    "liveChat": "雑談 (コメント枠)",
                    "ending": "終了'画面\\テスト",
                },
            )
            self.assertEqual(
                res["obs_media_sources"],
                {
                    "intro": "イントロ(BGM)",
                    "bgm": "BGM【通常】",
                    "outro": "アウトロ",
                    "keyboardVisualizer": "KeyboardVisualizer",
                },
            )
            self.assertEqual(
                res["obs_filter_names"],
                {
                    "bgmFadeIn": "フェードイン (In)",
                    "bgmFadeOut": "フェードアウト (Out)",
                },
            )
            self.assertEqual(
                res["obs_audio_sources"],
                {
                    "desktop": "デスクトップ音声（メイン）",
                },
            )

            # 返り値を書き換えてもキャッシュは汚染されない
            res["obs_scene_names"]["starting"] = "MUTATED"
            res2 = server_config.read_env_local_obs_names(env_file=env_path, cache=cache)
            self.assertEqual(res2["obs_scene_names"]["starting"], "開始画面（待機）")

            # ファイル更新 (size/mtime_ns 変化) で再読込される
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBS_LOCAL_ENV = { OBS_SCENE_NAMES: { starting: '更新後' } };\n")
            res3 = server_config.read_env_local_obs_names(env_file=env_path, cache=cache)
            self.assertEqual(res3["obs_scene_names"], {"starting": "更新後"})
            self.assertEqual(res3["obs_audio_sources"], {})


class TestApiConfigObsNamesHttp(unittest.TestCase):
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
            f.write(
                "window.OBS_LOCAL_ENV = {\n"
                "  OBS_DEFAULT_PASSWORD: 'env-local-obs-pw',\n"
                "  OBS_SCENE_NAMES: { starting: '開始（待機）', liveMain: 'メイン' },\n"
                "  OBS_MEDIA_SOURCES: { bgm: 'BGMソース' },\n"
                "  OBS_FILTER_NAMES: { bgmFadeIn: 'フェードイン' },\n"
                "  OBS_AUDIO_SOURCES: { desktop: 'デスクトップ音声' },\n"
                "};\n"
            )

        self.patch_cfg = mock.patch.object(server_config, "CONFIG_FILE", self.cfg_path)
        self.patch_env = mock.patch.object(server_config, "ENV_LOCAL_JS_FILE", self.env_local_path)
        self.patch_cfg.start()
        self.patch_env.start()
        server_config._CONFIG_CACHE["key"] = None
        server_config._CONFIG_CACHE["value"] = None
        server_config._ENV_LOCAL_CACHE["key"] = None
        server_config._ENV_LOCAL_CACHE["password"] = ""
        server_config._ENV_LOCAL_NAMES_CACHE["key"] = None
        server_config._ENV_LOCAL_NAMES_CACHE["value"] = None

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
        server_config._ENV_LOCAL_NAMES_CACHE["key"] = None
        server_config._ENV_LOCAL_NAMES_CACHE["value"] = None
        self.tmpdir.cleanup()

    def test_api_config_obs_names_access_control(self):
        name_keys = (
            "obs_scene_names",
            "obs_media_sources",
            "obs_filter_names",
            "obs_audio_sources",
        )

        # 1. localhost: 名前設定が返る
        status, _, raw = _raw_http_request(self.port, "GET", "/api/config")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["obs_scene_names"], {"starting": "開始（待機）", "liveMain": "メイン"})
        self.assertEqual(body["obs_media_sources"], {"bgm": "BGMソース"})
        self.assertEqual(body["obs_filter_names"], {"bgmFadeIn": "フェードイン"})
        self.assertEqual(body["obs_audio_sources"], {"desktop": "デスクトップ音声"})

        # 2. Origin: null: 名前設定フィールドが返らない
        status, _, raw = _raw_http_request(
            self.port, "GET", "/api/config", headers={"Origin": "null"}
        )
        self.assertEqual(status, 200)
        body = json.loads(raw)
        for k in name_keys:
            self.assertNotIn(k, body)

        # 3. LAN 未認証 (トークンなし): 名前設定フィールドが返らない
        with mock.patch.object(
            server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
        ):
            status, _, raw = _raw_http_request(self.port, "GET", "/api/config")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            for k in name_keys:
                self.assertNotIn(k, body)

            # 4. LAN 不正トークン: 名前設定フィールドが返らない
            status, _, raw = _raw_http_request(
                self.port,
                "GET",
                "/api/config",
                headers={"X-Widget-Token": "invalid-token"},
            )
            self.assertEqual(status, 200)
            body = json.loads(raw)
            for k in name_keys:
                self.assertNotIn(k, body)

            # 5. LAN 有効トークン (X-Widget-Token): 名前設定が返る
            status, _, raw = _raw_http_request(
                self.port,
                "GET",
                "/api/config",
                headers={"X-Widget-Token": "secret-api-token"},
            )
            self.assertEqual(status, 200)
            body = json.loads(raw)
            self.assertEqual(body["obs_scene_names"], {"starting": "開始（待機）", "liveMain": "メイン"})
            self.assertEqual(body["obs_media_sources"], {"bgm": "BGMソース"})
            self.assertEqual(body["obs_filter_names"], {"bgmFadeIn": "フェードイン"})
            self.assertEqual(body["obs_audio_sources"], {"desktop": "デスクトップ音声"})

    def test_lan_get_head_env_config_local_js_still_403(self):
        with mock.patch.object(
            server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
        ):
            for method in ("GET", "HEAD"):
                status, _, body = _raw_http_request(self.port, method, "/shared/env-config.local.js")
                self.assertEqual(status, 403, f"{method} /shared/env-config.local.js should be 403")
                if method == "HEAD":
                    self.assertEqual(body, b"")


class TestEnvConfigJsMergeAndStatus(unittest.TestCase):
    def test_env_config_js_in_place_merge_local_priority_and_status(self):
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync('shared/env-config.js', 'utf8');

async function runEnvConfig({ localEnv, fetchImpl }) {
  const ctx = {
    console,
    URL,
    location: {
      protocol: 'https:',
      hostname: '198.51.100.10',
      host: '198.51.100.10:8443',
      origin: 'https://198.51.100.10:8443',
      href: 'https://198.51.100.10:8443/Controller/index.html',
    },
    localStorage: { getItem: () => null },
    fetch: fetchImpl,
  };
  ctx.window = ctx;
  if (localEnv) ctx.window.OBS_LOCAL_ENV = localEnv;
  vm.runInNewContext(code, ctx);
  const env = ctx.window.OBSWidgetsEnv;
  const scenesRef = env.OBS_SCENE_NAMES;
  const mediaRef = env.OBS_MEDIA_SOURCES;
  const filterRef = env.OBS_FILTER_NAMES;
  const audioRef = env.OBS_AUDIO_SOURCES;
  await env.configReady;
  assert.strictEqual(env.OBS_SCENE_NAMES, scenesRef, 'OBS_SCENE_NAMES must be updated in-place');
  assert.strictEqual(env.OBS_MEDIA_SOURCES, mediaRef, 'OBS_MEDIA_SOURCES must be updated in-place');
  assert.strictEqual(env.OBS_FILTER_NAMES, filterRef, 'OBS_FILTER_NAMES must be updated in-place');
  assert.strictEqual(env.OBS_AUDIO_SOURCES, audioRef, 'OBS_AUDIO_SOURCES must be updated in-place');
  return env;
}

(async () => {
  // 1. 認証済み応答 (ok) + localEnv 優先
  const envOk = await runEnvConfig({
    localEnv: {
      OBS_SCENE_NAMES: { starting: 'ローカル優先（開始）' },
    },
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        obs_scene_names: { starting: 'サーバー側（無視される）', liveMain: 'メイン（配信）' },
        obs_media_sources: { bgm: 'BGM_JP' },
        obs_filter_names: { bgmFadeIn: 'フェードイン' },
        obs_audio_sources: { desktop: 'デスクトップ音声' },
        lan_ips: ['198.51.100.10'],
      }),
    }),
  });
  assert.strictEqual(envOk.configStatus, 'ok');
  assert.strictEqual(envOk.OBS_SCENE_NAMES.starting, 'ローカル優先（開始）');
  assert.strictEqual(envOk.OBS_SCENE_NAMES.liveMain, 'メイン（配信）');
  assert.strictEqual(envOk.OBS_MEDIA_SOURCES.bgm, 'BGM_JP');
  assert.strictEqual(envOk.OBS_FILTER_NAMES.bgmFadeIn, 'フェードイン');
  assert.strictEqual(envOk.OBS_AUDIO_SOURCES.desktop, 'デスクトップ音声');

  // 2. 未認証 LAN 応答 (200 だが名前フィールドなし -> unauthorized)
  const envUnauth200 = await runEnvConfig({
    localEnv: null,
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: 'ok', obs_password: '' }),
    }),
  });
  assert.strictEqual(envUnauth200.configStatus, 'unauthorized');
  assert.strictEqual(envUnauth200.OBS_SCENE_NAMES.liveMain, 'Live Main');

  // 3. HTTP 401/403 -> unauthorized
  const envUnauth403 = await runEnvConfig({
    localEnv: null,
    fetchImpl: async () => ({ ok: false, status: 403, json: async () => ({}) }),
  });
  assert.strictEqual(envUnauth403.configStatus, 'unauthorized');

  // 4. ネットワークエラー / 500 -> error
  const envNetErr = await runEnvConfig({
    localEnv: null,
    fetchImpl: async () => { throw new Error('network down'); },
  });
  assert.strictEqual(envNetErr.configStatus, 'error');

  const env500 = await runEnvConfig({
    localEnv: null,
    fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({}) }),
  });
  assert.strictEqual(env500.configStatus, 'error');
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
