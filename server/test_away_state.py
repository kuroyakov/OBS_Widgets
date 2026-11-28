#!/usr/bin/env python3
"""離席状態基盤 (server/away_state.py) と独立リアクタ (server/away_reactors.py) の単体テスト (Issue #75)。

実行:
    python3 server/test_away_state.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import away_reactors  # noqa: E402
from server import away_state  # noqa: E402
from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402
from server import subtitle_mute_link as sml  # noqa: E402
from server import voice_command  # noqa: E402
from server.obs_ws import ObsWsError  # noqa: E402


class _FakeObsClient:
    """Ticker / MicMute / SubtitleMuteLink テスト用の簡易フェイク OBS クライアント。"""

    def __init__(self, *, muted_inputs=None, fail_on=None, scene_items=None):
        self.muted_inputs = dict(muted_inputs or {})
        self.fail_on = dict(fail_on or {})
        self.scene_items = dict(scene_items or {})
        self.calls = []
        self.broadcasts = []

    def __enter__(self):
        if "connect" in self.fail_on:
            raise self.fail_on["connect"]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_input_mute(self, input_name):
        self.calls.append(("get_input_mute", input_name))
        if "get_input_mute" in self.fail_on:
            raise self.fail_on["get_input_mute"]
        if input_name not in self.muted_inputs:
            raise ObsWsError(f"GetInputMute failed: code=600 No input named {input_name}", code=600)
        return bool(self.muted_inputs[input_name])

    def set_input_mute(self, input_name, muted):
        self.calls.append(("set_input_mute", input_name, bool(muted)))
        if "set_input_mute" in self.fail_on:
            raise self.fail_on["set_input_mute"]
        self.muted_inputs[input_name] = bool(muted)

    def broadcast_custom_event(self, event_data):
        self.calls.append(("broadcast_custom_event", event_data.get("type")))
        if "broadcast_custom_event" in self.fail_on:
            raise self.fail_on["broadcast_custom_event"]
        self.broadcasts.append(event_data)

    def get_canvas_list(self):
        return []

    def get_scene_list(self, canvas_uuid=None):
        return [{"sceneName": name} for name in self.scene_items.keys()]

    def request(self, request_type, request_data=None):
        req = request_data or {}
        if request_type == "GetSceneItemList":
            scene = req["sceneName"]
            items = []
            for item in self.scene_items.get(scene, []):
                items.append(dict(item))
            return {"sceneItems": items}
        if request_type == "SetSceneItemEnabled":
            scene = req["sceneName"]
            item_id = req["sceneItemId"]
            enabled = bool(req["sceneItemEnabled"])
            self.calls.append(("SetSceneItemEnabled", scene, item_id, enabled))
            for item in self.scene_items.get(scene, []):
                if item["sceneItemId"] == item_id:
                    item["sceneItemEnabled"] = enabled
                    return {}
            raise ObsWsError("SetSceneItemEnabled failed: code=600", code=600)
        raise ObsWsError(f"Unsupported request: {request_type}", code=204)


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestAwayStateStoreUnit(unittest.TestCase):
    def setUp(self):
        self.store = away_state.AwayStateStore(time_fn=lambda: 1234.5)

    def tearDown(self):
        self.store.reset()

    def test_initial_state_and_transitions(self):
        init = self.store.get_state()
        self.assertEqual(
            init,
            {"active": False, "seq": 0, "updated_at": 0.0, "source": "", "reason": ""},
        )

        # OFF -> OFF は変化なしのため seq は増えない
        same_off = self.store.set_state(False, source="test")
        self.assertEqual(same_off["seq"], 0)
        self.assertFalse(same_off["active"])

        # OFF -> ON
        s1 = self.store.set_state(True, source="voice_command", reason="manual")
        self.assertEqual(
            s1,
            {
                "active": True,
                "seq": 1,
                "updated_at": 1234.5,
                "source": "voice_command",
                "reason": "manual",
            },
        )

        # ON -> ON は変化なし
        same_on = self.store.set_state(True, source="controller")
        self.assertEqual(same_on["seq"], 1)
        self.assertEqual(same_on["source"], "voice_command")

        # ON -> OFF
        s2 = self.store.set_state(False, source="controller", reason="scene_change")
        self.assertEqual(s2["seq"], 2)
        self.assertFalse(s2["active"])
        self.assertEqual(s2["source"], "controller")
        self.assertEqual(s2["reason"], "scene_change")

    def test_validation_rejects_invalid_args_without_side_effects(self):
        for kwargs in (
            {"active": "true", "source": "test"},
            {"active": 1, "source": "test"},
            {"active": True, "source": ""},
            {"active": True, "source": "   "},
            {"active": True, "source": 123},
            {"active": True, "source": "test", "reason": 456},
        ):
            with self.assertRaises(ValueError):
                self.store.set_state(**kwargs)
        self.assertEqual(self.store.get_state()["seq"], 0)
        self.assertFalse(self.store.get_state()["active"])

    def test_reactor_exception_isolation_does_not_rollback_or_block_others(self):
        received = []
        broken_calls = []

        def broken_reactor(state):
            broken_calls.append(state["seq"])
            raise RuntimeError("reactor boom")

        def healthy_reactor(state):
            received.append((state["seq"], state["active"], state["source"]))

        self.store.register_reactor("broken", broken_reactor)
        self.store.register_reactor("healthy", healthy_reactor)

        self.store.set_state(True, source="voice_command")
        self.assertTrue(self.store.wait_for_reactors(timeout=3.0))

        # 離席状態は巻き戻らない
        self.assertTrue(self.store.get_state()["active"])
        self.assertEqual(self.store.get_state()["seq"], 1)
        # 健全リアクタへ通知が届く
        self.assertEqual(received, [(1, True, "voice_command")])
        self.assertEqual(broken_calls, [1])

        # 例外を起こしたリアクタのスレッドも死なず次のイベントを受信できる (T4)
        self.store.set_state(False, source="voice_command")
        self.assertTrue(self.store.wait_for_reactors(timeout=3.0))
        self.assertFalse(self.store.get_state()["active"])
        self.assertEqual(received, [(1, True, "voice_command"), (2, False, "voice_command")])
        self.assertEqual(broken_calls, [1, 2])

    def test_slow_reactor_coalesces_to_latest_seq(self):
        started = threading.Event()
        release = threading.Event()
        seen_seqs = []

        def slow_reactor(state):
            seen_seqs.append(state["seq"])
            if state["seq"] == 1:
                started.set()
                release.wait(timeout=3.0)

        self.store.register_reactor("slow", slow_reactor)

        self.store.set_state(True, source="s1")   # seq=1
        self.assertTrue(started.wait(timeout=2.0))
        self.store.set_state(False, source="s2")  # seq=2 (合流でスキップされる)
        self.store.set_state(True, source="s3")   # seq=3 (合流でスキップされる)
        self.store.set_state(False, source="s4")  # seq=4 (最新)
        release.set()

        self.assertTrue(self.store.wait_for_reactors(timeout=3.0))
        self.assertEqual(seen_seqs[0], 1)
        self.assertEqual(seen_seqs[-1], 4)
        self.assertLess(len(seen_seqs), 4)

    def test_import_does_not_start_threads_or_register_reactors(self):
        code = (
            "import threading\n"
            "before = {t.name for t in threading.enumerate()}\n"
            "import server.away_state as s\n"
            "import server.away_reactors as r\n"
            "after = {t.name for t in threading.enumerate()}\n"
            "assert s.list_away_reactors() == [], s.list_away_reactors()\n"
            "assert after == before, (before, after)\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}")


class TestTickerAwayReactorUnit(unittest.TestCase):
    def test_on_off_saves_and_restores_ticker_and_respects_skip_reasons(self):
        tw_state = {"current_ticker": "通常配信中", "away_ticker_message": "Twitch側離席文言"}
        vc_cfg = {"away_message": "離席中テスト", "normal_message": "フォールバック通常"}
        fake_obs = _FakeObsClient()
        current_seq = [1]

        reactor = away_reactors.TickerAwayReactor(
            obs_client_factory=lambda: fake_obs,
            load_vc_config=lambda: dict(vc_cfg),
            load_tw_config=lambda: dict(tw_state),
            save_tw_config=lambda updates: tw_state.update(updates),
            get_current_seq=lambda: current_seq[0],
        )

        # ON: 現在文言を退避し離席文言を保存＆放送
        reactor.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
        self.assertEqual(tw_state["current_ticker"], "離席中テスト")
        self.assertEqual(fake_obs.broadcasts[-1]["type"], "UPDATE_TICKER")
        self.assertEqual(fake_obs.broadcasts[-1]["text"], "離席中テスト")

        # OFF (通常): 退避文言へ復帰
        current_seq[0] = 2
        reactor.on_away_state({"active": False, "seq": 2, "source": "voice_command", "reason": ""})
        self.assertEqual(tw_state["current_ticker"], "通常配信中")
        self.assertEqual(fake_obs.broadcasts[-1]["text"], "通常配信中")

        # 再度 ON -> OFF (reason=scene_change): 新文言を上書きしない (T2)
        current_seq[0] = 3
        reactor.on_away_state({"active": True, "seq": 3, "source": "voice_command", "reason": ""})
        self.assertEqual(tw_state["current_ticker"], "離席中テスト")
        tw_state["current_ticker"] = "新シーンの文言"
        before_broadcast_count = len(fake_obs.broadcasts)

        current_seq[0] = 4
        reactor.on_away_state({"active": False, "seq": 4, "source": "controller", "reason": "scene_change"})
        self.assertEqual(tw_state["current_ticker"], "新シーンの文言")
        self.assertEqual(len(fake_obs.broadcasts), before_broadcast_count)

        # 再度 ON -> OFF (reason=manual_ticker_change): 新文言を上書きしない (T2)
        current_seq[0] = 5
        reactor.on_away_state({"active": True, "seq": 5, "source": "controller", "reason": ""})
        tw_state["current_ticker"] = "手動変更の文言"
        before_broadcast_count = len(fake_obs.broadcasts)

        current_seq[0] = 6
        reactor.on_away_state({"active": False, "seq": 6, "source": "controller", "reason": "manual_ticker_change"})
        self.assertEqual(tw_state["current_ticker"], "手動変更の文言")
        self.assertEqual(len(fake_obs.broadcasts), before_broadcast_count)

    def test_stale_seq_does_not_overwrite_newer_ticker(self):
        tw_state = {"current_ticker": "最新文言"}
        fake_obs = _FakeObsClient()
        reactor = away_reactors.TickerAwayReactor(
            obs_client_factory=lambda: fake_obs,
            load_vc_config=lambda: {"away_message": "離席中", "normal_message": "通常"},
            load_tw_config=lambda: dict(tw_state),
            save_tw_config=lambda updates: tw_state.update(updates),
            get_current_seq=lambda: 5,
        )
        # seq=2 は古いので無視される (T1)
        reactor.on_away_state({"active": True, "seq": 2, "source": "voice_command", "reason": ""})
        self.assertEqual(tw_state["current_ticker"], "最新文言")
        self.assertEqual(fake_obs.broadcasts, [])


class TestMicMuteAwayReactorUnit(unittest.TestCase):
    def test_mute_unmute_only_when_muted_by_away_and_fallback_to_subtitle_mic(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "away_mic_mute_state.json")
            fake_obs = _FakeObsClient(muted_inputs={"Mic A": False})
            vc_cfg = {
                "away_mic_mute": {"enabled": True, "input_name": ""},
                "subtitle_mute_link": {"enabled": True, "input_name": "Mic A", "source_names": ["Sub"]},
            }
            seq = [1]
            reactor = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: fake_obs,
                load_vc_config=lambda: dict(vc_cfg),
                state_path=state_path,
                get_current_seq=lambda: seq[0],
            )

            # ON: 未ミュートなのでミュートし、状態ファイルへ記録
            reactor.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertTrue(fake_obs.muted_inputs["Mic A"])
            self.assertTrue(os.path.isfile(state_path))
            rec = away_reactors.load_mic_mute_state(state_path=state_path)
            self.assertEqual(rec["input_name"], "Mic A")

            # OFF: 記録があるので解除し、状態ファイルを削除
            seq[0] = 2
            reactor.on_away_state({"active": False, "seq": 2, "source": "voice_command", "reason": ""})
            self.assertFalse(fake_obs.muted_inputs["Mic A"])
            self.assertFalse(os.path.exists(state_path))

    def test_manual_mute_is_preserved_across_away_on_and_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "away_mic_mute_state.json")
            fake_obs = _FakeObsClient(muted_inputs={"Mic A": True})
            vc_cfg = {"away_mic_mute": {"enabled": True, "input_name": "Mic A"}}
            seq = [1]
            reactor = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: fake_obs,
                load_vc_config=lambda: dict(vc_cfg),
                state_path=state_path,
                get_current_seq=lambda: seq[0],
            )

            # ON: 既に手動ミュート済みなので SetInputMute も記録もしない
            reactor.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertTrue(fake_obs.muted_inputs["Mic A"])
            self.assertFalse(os.path.exists(state_path))
            self.assertNotIn(("set_input_mute", "Mic A", True), fake_obs.calls)

            # OFF: 記録がないので解除しない
            seq[0] = 2
            reactor.on_away_state({"active": False, "seq": 2, "source": "voice_command", "reason": ""})
            self.assertTrue(fake_obs.muted_inputs["Mic A"])
            self.assertNotIn(("set_input_mute", "Mic A", False), fake_obs.calls)

    def test_obs_disconnected_missing_input_disabled_and_corrupt_state_are_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "away_mic_mute_state.json")
            # OBS 未接続
            disconnected_obs = _FakeObsClient(fail_on={"connect": ObsWsError("OBS 未接続")})
            reactor = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: disconnected_obs,
                load_vc_config=lambda: {"away_mic_mute": {"enabled": True, "input_name": "Mic A"}},
                state_path=state_path,
                get_current_seq=lambda: 1,
            )
            reactor.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertFalse(os.path.exists(state_path))

            # 対象マイク未指定
            fake_obs = _FakeObsClient(muted_inputs={"Mic A": False})
            reactor_no_input = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: fake_obs,
                load_vc_config=lambda: {"away_mic_mute": {"enabled": True, "input_name": ""}},
                state_path=state_path,
                get_current_seq=lambda: 1,
            )
            reactor_no_input.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertEqual(fake_obs.calls, [])
            self.assertFalse(os.path.exists(state_path))

            # enabled=False
            reactor_disabled = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: fake_obs,
                load_vc_config=lambda: {"away_mic_mute": {"enabled": False, "input_name": "Mic A"}},
                state_path=state_path,
                get_current_seq=lambda: 1,
            )
            reactor_disabled.on_away_state({"active": True, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertEqual(fake_obs.calls, [])

            # 破損状態ファイルでもクラッシュせず no-op (T4)
            with open(state_path, "w", encoding="utf-8") as f:
                f.write("{broken json")
            reactor.on_away_state({"active": False, "seq": 1, "source": "voice_command", "reason": ""})
            self.assertIsNone(away_reactors.load_mic_mute_state(state_path=state_path))

    def test_off_keeps_record_on_transient_error_and_clears_on_permanent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "away_mic_mute_state.json")
            away_reactors.save_mic_mute_state("Mic A", state_path=state_path)

            # 一時エラー (207 NotReady) -> 記録を保持 (T2/T6)
            transient_obs = _FakeObsClient(
                muted_inputs={"Mic A": True},
                fail_on={"set_input_mute": ObsWsError("SetInputMute failed: code=207 NotReady", code=207)},
            )
            reactor_t = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: transient_obs,
                load_vc_config=lambda: {"away_mic_mute": {"enabled": True, "input_name": "Mic A"}},
                state_path=state_path,
                get_current_seq=lambda: 2,
            )
            reactor_t.on_away_state({"active": False, "seq": 2, "source": "voice_command", "reason": ""})
            self.assertIsNotNone(away_reactors.load_mic_mute_state(state_path=state_path))

            # 恒久エラー (600 ResourceNotFound) -> 記録を破棄 (T6)
            permanent_obs = _FakeObsClient(
                muted_inputs={"Mic A": True},
                fail_on={"set_input_mute": ObsWsError("SetInputMute failed: code=600 ResourceNotFound", code=600)},
            )
            reactor_p = away_reactors.MicMuteAwayReactor(
                obs_client_factory=lambda: permanent_obs,
                load_vc_config=lambda: {"away_mic_mute": {"enabled": True, "input_name": "Mic A"}},
                state_path=state_path,
                get_current_seq=lambda: 2,
            )
            reactor_p.on_away_state({"active": False, "seq": 2, "source": "voice_command", "reason": ""})
            self.assertFalse(os.path.exists(state_path))

    def test_default_state_file_constant_path(self):
        # T7: デフォルト引数 (state_path=None) で本番定数 AWAY_MIC_MUTE_STATE_FILE を使う経路を踏む
        with tempfile.TemporaryDirectory() as tmp:
            default_path = os.path.join(tmp, "away_mic_mute_state.json")
            with mock.patch.object(away_reactors, "AWAY_MIC_MUTE_STATE_FILE", default_path):
                away_reactors.save_mic_mute_state("Default Mic")
                self.assertTrue(os.path.isfile(default_path))
                loaded = away_reactors.load_mic_mute_state()
                self.assertEqual(loaded["input_name"], "Default Mic")
                away_reactors.clear_mic_mute_state()
                self.assertFalse(os.path.exists(default_path))


class TestEndToEndVoiceAwayFlowUnit(unittest.TestCase):
    def setUp(self):
        away_state.reset_away_state()
        voice_command._LAST_EXEC_TS = None
        voice_command._HISTORY.clear()

    def tearDown(self):
        away_reactors.stop_away_reactors()
        away_state.reset_away_state()

    def test_voice_command_away_on_off_integrates_ticker_mic_and_subtitle_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            vc_cfg_path = os.path.join(tmp, "voice_command_config.json")
            tw_cfg_path = os.path.join(tmp, "twitch_config.json")
            mic_state_path = os.path.join(tmp, "away_mic_mute_state.json")
            sml_state_path = os.path.join(tmp, "subtitle_mute_link_state.json")

            with open(tw_cfg_path, "w", encoding="utf-8") as f:
                json.dump({"current_ticker": "通常ティッカー"}, f)

            voice_command.save_voice_command_config(
                {
                    "enabled": True,
                    "wake_words": ["コマンド"],
                    "cooldown_sec": 0.0,
                    "away_message": "離席中です",
                    "custom_commands": [
                        {"aliases": ["離席"], "type": "away", "mode": "on"},
                        {"aliases": ["戻り"], "type": "away", "mode": "off"},
                    ],
                    "subtitle_mute_link": {
                        "enabled": True,
                        "input_name": "Main Mic",
                        "source_names": ["Subtitle Active", "Subtitle ManualHidden"],
                    },
                    "away_mic_mute": {
                        "enabled": True,
                        "input_name": "",
                    },
                },
                config_path=vc_cfg_path,
            )

            fake_obs = _FakeObsClient(
                muted_inputs={"Main Mic": False},
                scene_items={
                    "Main Scene": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Active", "sceneItemEnabled": True},
                        {"sceneItemId": 2, "sourceName": "Subtitle ManualHidden", "sceneItemEnabled": False},
                    ]
                },
            )
            link_cfg = {
                "enabled": True,
                "input_name": "Main Mic",
                "source_names": ["Subtitle Active", "Subtitle ManualHidden"],
            }

            with (
                mock.patch.object(server_config, "CONFIG_FILE", tw_cfg_path),
                mock.patch.object(voice_command, "VOICE_COMMAND_CONFIG_FILE", vc_cfg_path),
            ):
                server_config._CONFIG_CACHE["key"] = None
                server_config._CONFIG_CACHE["value"] = None
                voice_command._CONFIG_CACHE["key"] = None
                voice_command._CONFIG_CACHE["value"] = None

                away_reactors.start_away_reactors(
                    obs_client_factory=lambda: fake_obs,
                    load_vc_config=lambda: voice_command.load_voice_command_config(config_path=vc_cfg_path),
                    state_path=mic_state_path,
                )

                # 1. 「離席」発話 -> 離席状態 ON -> ティッカー切替 + マイクミュート
                r_on = voice_command.process_utterance(
                    "コマンド、離席",
                    now=10.0,
                    obs_client_factory=lambda: fake_obs,
                    config_path=vc_cfg_path,
                )
                self.assertTrue(r_on["ok"])
                self.assertTrue(away_state.wait_for_away_reactors(timeout=3.0))
                self.assertTrue(away_state.get_away_state()["active"])
                self.assertEqual(server_config.load_twitch_config()["current_ticker"], "離席中です")
                self.assertTrue(fake_obs.muted_inputs["Main Mic"])

                # 既存 subtitle_mute_link が独立にマイクミュート状態へ反応して字幕を隠す
                sml.apply_mute_state(
                    fake_obs,
                    link_cfg["input_name"],
                    link_cfg["source_names"],
                    sml_state_path,
                    fake_obs.muted_inputs["Main Mic"],
                )
                items = {it["sourceName"]: it["sceneItemEnabled"] for it in fake_obs.scene_items["Main Scene"]}
                self.assertFalse(items["Subtitle Active"])
                self.assertFalse(items["Subtitle ManualHidden"])

                # 2. 「戻り」発話 -> 離席状態 OFF -> ティッカー復帰 + マイクミュート解除
                r_off = voice_command.process_utterance(
                    "コマンド、戻り",
                    now=20.0,
                    obs_client_factory=lambda: fake_obs,
                    config_path=vc_cfg_path,
                )
                self.assertTrue(r_off["ok"])
                self.assertTrue(away_state.wait_for_away_reactors(timeout=3.0))
                self.assertFalse(away_state.get_away_state()["active"])
                self.assertEqual(server_config.load_twitch_config()["current_ticker"], "通常ティッカー")
                self.assertFalse(fake_obs.muted_inputs["Main Mic"])

                # 既存 subtitle_mute_link が自分が隠した字幕だけを復帰し、手動非表示は維持
                sml.apply_mute_state(
                    fake_obs,
                    link_cfg["input_name"],
                    link_cfg["source_names"],
                    sml_state_path,
                    fake_obs.muted_inputs["Main Mic"],
                )
                items = {it["sourceName"]: it["sceneItemEnabled"] for it in fake_obs.scene_items["Main Scene"]}
                self.assertTrue(items["Subtitle Active"])
                self.assertFalse(items["Subtitle ManualHidden"])


class TestAwayStateHttpApi(unittest.TestCase):
    def setUp(self):
        away_state.reset_away_state()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        away_state.reset_away_state()

    def test_get_and_post_away_state_and_security_guards(self):
        # 1. GET /api/away-state は初期状態を返す
        st, body = _http_request(f"{self.base_url}/api/away-state", method="GET")
        self.assertEqual(st, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["active"], False)
        self.assertEqual(data["seq"], 0)

        # 2. localhost からの POST /api/away-state で状態変更
        st, body = _http_request(
            f"{self.base_url}/api/away-state",
            method="POST",
            body=json.dumps({"active": True, "source": "controller", "reason": "button"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["active"])
        self.assertEqual(data["seq"], 1)
        self.assertEqual(data["source"], "controller")
        self.assertEqual(data["reason"], "button")

        # 3. 不正入力は 400 で状態変更なし (T3, T7)
        for bad in (
            {"active": "true", "source": "x"},
            {"active": False, "source": "   "},
            {"active": False, "source": "x", "reason": 123},
        ):
            st, _ = _http_request(
                f"{self.base_url}/api/away-state",
                method="POST",
                body=json.dumps(bad).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(st, 400)
        self.assertEqual(away_state.get_away_state()["seq"], 1)

        # 4. 非 localhost ではトークン必須（GET は読み取り専用のため 200）
        fake_token = "dummy-away-token"
        with (
            mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False),
            mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token),
            mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token),
        ):
            st_get, _ = _http_request(f"{self.base_url}/api/away-state", method="GET")
            self.assertEqual(st_get, 200)

            st_401, _ = _http_request(
                f"{self.base_url}/api/away-state",
                method="POST",
                body=json.dumps({"active": False, "source": "lan"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(st_401, 401)
            self.assertEqual(away_state.get_away_state()["seq"], 1)

            st_200, body_200 = _http_request(
                f"{self.base_url}/api/away-state",
                method="POST",
                body=json.dumps({"active": False, "source": "lan", "reason": "done"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Widget-Token": fake_token},
            )
            self.assertEqual(st_200, 200)
            self.assertFalse(json.loads(body_200.decode("utf-8"))["active"])

        # 5. away_mic_mute_state.json と一時ファイルは静的配信ブロック (403)
        for path in ("/away_mic_mute_state.json", "/away_mic_mute_state.json.tmp.123"):
            st_g, _ = _http_request(f"{self.base_url}{path}", method="GET")
            self.assertEqual(st_g, 403, f"GET {path} must be 403")
            st_h, _ = _http_request(f"{self.base_url}{path}", method="HEAD")
            self.assertEqual(st_h, 403, f"HEAD {path} must be 403")


if __name__ == "__main__":
    unittest.main(verbosity=2)
