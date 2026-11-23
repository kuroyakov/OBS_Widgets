#!/usr/bin/env python3
"""VoiceCommand モジュール (server/voice_command.py) と HTTP API の単体テスト (Issue #29)。

実行:
    python3 server/test_voice_command.py
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402
from server import voice_command  # noqa: E402


class _FakeObsClient:
    def __init__(self, initial_db=-10.0):
        self.volume_db = initial_db
        self.muted = False
        self.calls = []
        self.broadcasts = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_input_volume(self, input_name):
        self.calls.append(("get_input_volume", input_name))
        return {"inputVolumeDb": self.volume_db, "inputVolumeMul": 0.5}

    def set_input_volume(self, input_name, volume_db=None, volume_mul=None):
        self.volume_db = float(volume_db)
        self.calls.append(("set_input_volume", input_name, self.volume_db))

    def set_input_mute(self, input_name, muted):
        self.muted = bool(muted)
        self.calls.append(("set_input_mute", input_name, self.muted))

    def set_source_filter_enabled(self, source_name, filter_name, enabled):
        self.calls.append(("set_source_filter_enabled", source_name, filter_name, bool(enabled)))

    def trigger_hotkey_by_name(self, hotkey_name, context_name=None):
        self.calls.append(("trigger_hotkey_by_name", hotkey_name))

    def broadcast_custom_event(self, event_data):
        self.broadcasts.append(event_data)


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestVoiceCommandLogicUnit(unittest.TestCase):
    def setUp(self):
        voice_command._LAST_EXEC_TS = 0.0
        voice_command._SAVED_TICKER_BEFORE_AWAY = None
        voice_command._HISTORY.clear()
        voice_command._CONFIG_CACHE["key"] = None
        voice_command._CONFIG_CACHE["value"] = None

    def test_longest_match_prefers_pin_mic_over_mic_and_unmute_over_mute(self):
        cfg = {
            "wake_words": ["コマンド"],
            "audio_targets": [
                {"aliases": ["マイク"], "input_name": "Main Mic"},
                {"aliases": ["ピンマイク"], "input_name": "Pin Mic"},
            ],
        }
        m1 = voice_command.match_command("コマンド、ピンマイク上げて", cfg=cfg)
        self.assertIsNotNone(m1)
        self.assertEqual(m1["input_name"], "Pin Mic")
        self.assertEqual(m1["action"], "volume_up")

        m2 = voice_command.match_command("コマンド マイク ミュート解除", cfg=cfg)
        self.assertIsNotNone(m2)
        self.assertEqual(m2["input_name"], "Main Mic")
        self.assertEqual(m2["action"], "unmute")

        self.assertIsNone(voice_command.match_command("普通の会話 ピンマイク上げて", cfg=cfg))

    def test_volume_clamp_cooldown_and_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "voice_command_config.json")
            voice_command.save_voice_command_config({
                "enabled": True,
                "wake_words": ["コマンド"],
                "cooldown_sec": 2.0,
                "volume_step_db": 5.0,
                "volume_min_db": -20.0,
                "volume_max_db": 0.0,
                "audio_targets": [{"aliases": ["ゲーム音"], "input_name": "Game Audio"}],
            }, config_path=cfg_path)

            fake_obs = _FakeObsClient(initial_db=-3.0)
            r1 = voice_command.process_utterance(
                "コマンド、ゲーム音上げて",
                now=10.0,
                obs_client_factory=lambda: fake_obs,
                config_path=cfg_path,
            )
            self.assertTrue(r1["executed"] and r1["ok"])
            self.assertEqual(fake_obs.volume_db, 0.0)  # +5.0 -> 0.0 で上限ガード
            self.assertEqual(fake_obs.broadcasts[-1]["realm"], "obs-voice-command-v1")
            self.assertEqual(fake_obs.broadcasts[-1]["type"], "VOICE_COMMAND_RESULT")

            # クールダウン内は拒否
            r_cool = voice_command.process_utterance(
                "コマンド、ゲーム音下げて",
                now=11.0,
                obs_client_factory=lambda: fake_obs,
                config_path=cfg_path,
            )
            self.assertTrue(r_cool["matched"])
            self.assertFalse(r_cool["executed"])
            self.assertEqual(r_cool["reason"], "cooldown")

            # テスト実行時はクールダウンをバイパスし下限ガードも機能
            fake_obs.volume_db = -18.0
            r_test = voice_command.process_utterance(
                "コマンド、ゲーム音下げて",
                is_test=True,
                now=11.2,
                obs_client_factory=lambda: fake_obs,
                config_path=cfg_path,
            )
            self.assertTrue(r_test["executed"] and r_test["ok"])
            self.assertEqual(fake_obs.volume_db, -20.0)

    def test_filter_hotkey_and_away_mode_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            vc_cfg = os.path.join(tmp, "voice_command_config.json")
            tw_cfg = os.path.join(tmp, "twitch_config.json")
            with open(tw_cfg, "w", encoding="utf-8") as f:
                json.dump({"current_ticker": "元のティッカー文面"}, f)

            voice_command.save_voice_command_config({
                "enabled": True,
                "wake_words": ["コマンド"],
                "away_message": "離席中です",
                "custom_commands": [
                    {
                        "aliases": ["白黒"],
                        "type": "filter",
                        "source_name": "GameCap",
                        "filter_name": "Grayscale",
                        "enabled": True,
                        "disable_filters": ["Sepia"],
                    },
                    {"aliases": ["録画開始"], "type": "hotkey", "hotkey_name": "OBSBasic.StartRecording"},
                    {"aliases": ["離席"], "type": "away", "mode": "on"},
                    {"aliases": ["戻り"], "type": "away", "mode": "off"},
                ],
            }, config_path=vc_cfg)

            fake_obs = _FakeObsClient()
            with mock.patch.object(server_config, "CONFIG_FILE", tw_cfg):
                server_config._CONFIG_CACHE["key"] = None
                server_config._CONFIG_CACHE["value"] = None

                r_flt = voice_command.process_utterance(
                    "コマンド、白黒", now=10.0, obs_client_factory=lambda: fake_obs, config_path=vc_cfg
                )
                self.assertTrue(r_flt["ok"])
                self.assertIn(("set_source_filter_enabled", "GameCap", "Sepia", False), fake_obs.calls)
                self.assertIn(("set_source_filter_enabled", "GameCap", "Grayscale", True), fake_obs.calls)

                r_hk = voice_command.process_utterance(
                    "コマンド、録画開始", now=20.0, obs_client_factory=lambda: fake_obs, config_path=vc_cfg
                )
                self.assertTrue(r_hk["ok"])
                self.assertIn(("trigger_hotkey_by_name", "OBSBasic.StartRecording"), fake_obs.calls)

                r_away_on = voice_command.process_utterance(
                    "コマンド、離席", now=30.0, obs_client_factory=lambda: fake_obs, config_path=vc_cfg
                )
                self.assertTrue(r_away_on["ok"])
                self.assertEqual(server_config.load_twitch_config()["current_ticker"], "離席中です")
                self.assertTrue(any(
                    b.get("realm") == "obs-stream-ticker-v1" and b.get("text") == "離席中です"
                    for b in fake_obs.broadcasts
                ))

                r_away_off = voice_command.process_utterance(
                    "コマンド、戻り", now=40.0, obs_client_factory=lambda: fake_obs, config_path=vc_cfg
                )
                self.assertTrue(r_away_off["ok"])
                self.assertEqual(server_config.load_twitch_config()["current_ticker"], "元のティッカー文面")

    def test_corrupt_config_is_backed_up_and_hot_reload_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "voice_command_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("{broken")

            with mock.patch.object(voice_command, "VOICE_COMMAND_CONFIG_FILE", cfg_path):
                voice_command._CONFIG_CACHE["key"] = None
                voice_command._CONFIG_CACHE["value"] = None
                loaded = voice_command.load_voice_command_config()
                self.assertTrue(loaded["enabled"])
                self.assertTrue(os.path.isfile(cfg_path + ".corrupt"))

                voice_command.save_voice_command_config({"wake_words": ["ボット"]})
                reloaded = voice_command.load_voice_command_config()
                self.assertEqual(reloaded["wake_words"], ["ボット"])

    def test_volume_recovers_from_none_and_neg_inf(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "voice_command_config.json")
            voice_command.save_voice_command_config(
                {
                    "enabled": True,
                    "wake_words": ["コマンド"],
                    "cooldown_sec": 0.0,
                    "volume_step_db": 3.0,
                    "volume_min_db": -60.0,
                    "volume_max_db": 0.0,
                    "audio_targets": [{"aliases": ["BGM"], "input_name": "BGM"}],
                },
                config_path=cfg_path,
            )
            for initial_db in (None, float("-inf")):
                fake_obs = _FakeObsClient(initial_db=initial_db)
                res = voice_command.process_utterance(
                    "コマンド、BGM上げて",
                    now=100.0,
                    obs_client_factory=lambda: fake_obs,
                    config_path=cfg_path,
                )
                self.assertTrue(res["executed"])
                self.assertTrue(res["ok"])
                self.assertEqual(fake_obs.volume_db, -57.0)

    def test_non_finite_config_values_rejected_and_normalized(self):
        norm = voice_command._normalize_config({
            "cooldown_sec": float("nan"),
            "volume_step_db": float("inf"),
            "volume_min_db": float("-inf"),
            "volume_max_db": 0.0,
        })
        self.assertEqual(norm["cooldown_sec"], voice_command.DEFAULT_VOICE_COMMAND_CONFIG["cooldown_sec"])
        self.assertEqual(norm["volume_step_db"], voice_command.DEFAULT_VOICE_COMMAND_CONFIG["volume_step_db"])
        self.assertEqual(norm["volume_min_db"], voice_command.DEFAULT_VOICE_COMMAND_CONFIG["volume_min_db"])

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "voice_command_config.json")
            for bad_payload in (
                {"cooldown_sec": float("nan")},
                {"volume_step_db": float("inf")},
                {"volume_min_db": float("-inf")},
                {"volume_max_db": float("nan")},
            ):
                with self.assertRaises(ValueError):
                    voice_command.save_voice_command_config(bad_payload, config_path=cfg_path)

    def test_is_test_does_not_trigger_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "voice_command_config.json")
            voice_command.save_voice_command_config(
                {
                    "enabled": True,
                    "wake_words": ["コマンド"],
                    "cooldown_sec": 5.0,
                    "audio_targets": [{"aliases": ["BGM"], "input_name": "BGM"}],
                },
                config_path=cfg_path,
            )
            voice_command._LAST_EXEC_TS = None
            fake_obs = _FakeObsClient(initial_db=-10.0)
            res_test = voice_command.process_utterance(
                "コマンド、BGM上げて",
                is_test=True,
                now=10.0,
                obs_client_factory=lambda: fake_obs,
                config_path=cfg_path,
            )
            self.assertTrue(res_test["executed"])
            self.assertIsNone(voice_command._LAST_EXEC_TS)

            res_real = voice_command.process_utterance(
                "コマンド、BGM上げて",
                is_test=False,
                now=10.5,
                obs_client_factory=lambda: fake_obs,
                config_path=cfg_path,
            )
            self.assertTrue(res_real["executed"])
            self.assertEqual(voice_command._LAST_EXEC_TS, 10.5)

    def test_default_obs_client_factory_env_local_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "env-config.local.js")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'secret-from-env-local' };\n")

            with (
                mock.patch.object(voice_command, "_ENV_LOCAL_JS_FILE", env_path),
                mock.patch.dict(voice_command._ENV_LOCAL_CACHE, {"key": None, "password": ""}),
                mock.patch.object(voice_command, "load_twitch_config", return_value={}),
                mock.patch.object(voice_command, "ObsWebSocketClient") as mock_ws,
            ):
                voice_command._default_obs_client_factory()
                mock_ws.assert_called_once_with(
                    host=voice_command.OBS_WS_UPSTREAM_HOST,
                    port=voice_command.OBS_WS_UPSTREAM_PORT,
                    password="secret-from-env-local",
                    timeout=4.0,
                )

            # YOUR_OBS_PASSWORD プレースホルダーは無視される
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'YOUR_OBS_PASSWORD' };\n")
            with (
                mock.patch.object(voice_command, "_ENV_LOCAL_JS_FILE", env_path),
                mock.patch.dict(voice_command._ENV_LOCAL_CACHE, {"key": None, "password": ""}),
                mock.patch.object(voice_command, "load_twitch_config", return_value={}),
                mock.patch.object(voice_command, "ObsWebSocketClient") as mock_ws,
            ):
                voice_command._default_obs_client_factory()
                mock_ws.assert_called_once_with(
                    host=voice_command.OBS_WS_UPSTREAM_HOST,
                    port=voice_command.OBS_WS_UPSTREAM_PORT,
                    password="",
                    timeout=4.0,
                )

            # twitch_config.json の obs_password があればそちらを優先する
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("window.OBSWidgetsEnv = { OBS_DEFAULT_PASSWORD: 'secret-from-env-local' };\n")
            with (
                mock.patch.object(voice_command, "_ENV_LOCAL_JS_FILE", env_path),
                mock.patch.dict(voice_command._ENV_LOCAL_CACHE, {"key": None, "password": ""}),
                mock.patch.object(voice_command, "load_twitch_config", return_value={"obs_password": "twitch-pw"}),
                mock.patch.object(voice_command, "ObsWebSocketClient") as mock_ws,
            ):
                voice_command._default_obs_client_factory()
                mock_ws.assert_called_once_with(
                    host=voice_command.OBS_WS_UPSTREAM_HOST,
                    port=voice_command.OBS_WS_UPSTREAM_PORT,
                    password="twitch-pw",
                    timeout=4.0,
                )


class TestVoiceCommandHttpSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "voice_command_config.json")
        voice_command.save_voice_command_config({"enabled": True}, config_path=self.cfg_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def test_all_endpoints_require_local_or_token_and_config_file_blocked(self):
        # 直接 GET / HEAD は 403
        for path in ("/voice_command_config.json", "/voice_command_config.json.corrupt", "/voice_command_config.json.tmp.1"):
            st_get, _ = _http_request(f"{self.base_url}{path}", method="GET")
            self.assertEqual(st_get, 403)
            st_head, _ = _http_request(f"{self.base_url}{path}", method="HEAD")
            self.assertEqual(st_head, 403)

        fake_token = "dummy-vc-token"
        with (
            mock.patch.object(voice_command, "VOICE_COMMAND_CONFIG_FILE", self.cfg_path),
            mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False),
            mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token),
            mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token),
        ):
            for ep in ("/api/voice-command/config", "/api/voice-command/obs-resources", "/api/voice-command/history"):
                st, _ = _http_request(f"{self.base_url}{ep}", method="GET")
                self.assertEqual(st, 401, f"GET {ep} without token should be 401")
                st_ok, _ = _http_request(f"{self.base_url}{ep}", method="GET", headers={"X-Widget-Token": fake_token})
                self.assertEqual(st_ok, 200, f"GET {ep} with token should be 200")

            for ep, payload in (
                ("/api/voice-command/config", {"enabled": True}),
                ("/api/voice-command/utterance", {"text": "雑談"}),
                ("/api/voice-command/test", {"text": "雑談"}),
            ):
                body = json.dumps(payload).encode("utf-8")
                st, _ = _http_request(
                    f"{self.base_url}{ep}",
                    method="POST",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(st, 401, f"POST {ep} without token should be 401")
                st_ok, _ = _http_request(
                    f"{self.base_url}{ep}",
                    method="POST",
                    body=body,
                    headers={"Content-Type": "application/json", "X-Widget-Token": fake_token},
                )
                self.assertEqual(st_ok, 200, f"POST {ep} with token should be 200")


if __name__ == "__main__":
    unittest.main(verbosity=2)
