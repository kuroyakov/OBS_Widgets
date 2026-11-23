#!/usr/bin/env python3
"""
字幕ソース連動モジュール (server/subtitle_mute_link.py) の単体テスト (Issue #39)
実行: python3 server/test_subtitle_mute_link.py
"""

import copy
import json
import os
import queue
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

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402
from server import subtitle_mute_link as sml  # noqa: E402
from server import voice_command  # noqa: E402
from server.obs_ws import ObsWsError  # noqa: E402


class _FakeObsClient:
    """テスト用のフェイク OBS WebSocket クライアント。"""

    def __init__(self, *, scenes=None, mutes=None, canvases=None, canvas_scenes=None):
        # scenes: {scene_name: [{"sceneItemId": int, "sourceName": str, "sceneItemEnabled": bool}, ...]}
        # canvases: [{"canvasUuid": str, "canvasName": str}, ...] or list of strings
        # canvas_scenes: {canvas_uuid: {scene_name: [scene_items...]}}
        self.scenes = copy.deepcopy(scenes or {})
        self.mutes = dict(mutes or {})
        self.canvases = copy.deepcopy(canvases) if canvases is not None else None
        self.canvas_scenes = copy.deepcopy(canvas_scenes or {})
        self.calls = []
        self.connected = False
        self.closed = False
        self.events = queue.Queue()
        self.connect_hook = None
        self.get_input_mute_hook = None
        self.get_scene_item_list_hook = None
        self.set_scene_item_enabled_hook = None
        self.get_canvas_list_hook = None
        self.connected_event = threading.Event()
        self.reconcile_done_event = threading.Event()

    def connect(self):
        if self.connect_hook is not None:
            self.connect_hook(self)
        self.connected = True
        self.connected_event.set()

    def close(self):
        self.closed = True
        self.connected = False
        self.events.put(None)

    def push_event(self, event_type, event_data):
        self.events.put({
            "op": 5,
            "d": {
                "eventType": event_type,
                "eventData": event_data,
            },
        })

    def poll_message(self, timeout=0.5):
        self.reconcile_done_event.set()
        if self.closed:
            raise ObsWsError("WebSocket が切断されました")
        try:
            msg = self.events.get(timeout=timeout)
        except queue.Empty:
            return None
        if msg is None:
            raise ObsWsError("WebSocket が切断されました")
        return msg

    def get_input_mute(self, input_name):
        self.calls.append(("GetInputMute", input_name))
        if self.get_input_mute_hook is not None:
            res = self.get_input_mute_hook(self, input_name)
            if res is not None:
                return res
        if input_name not in self.mutes:
            raise ObsWsError(
                f"GetInputMute が失敗しました: code=600 No input named {input_name}",
                code=600,
                comment="No input",
            )
        return bool(self.mutes[input_name])

    def get_canvas_list(self):
        self.calls.append(("GetCanvasList",))
        if self.get_canvas_list_hook is not None:
            res = self.get_canvas_list_hook(self)
            if res is not None:
                return res
        if self.canvases is None:
            # 従来クライアント（GetCanvasList 未対応）の挙動を模倣
            raise ObsWsError(
                "GetCanvasList が失敗しました: code=204 UnknownRequestType",
                code=204,
                comment="UnknownRequestType",
            )
        return copy.deepcopy(self.canvases)

    def get_scene_list(self, canvas_uuid=None):
        self.calls.append(("GetSceneList", canvas_uuid))
        if canvas_uuid and canvas_uuid in self.canvas_scenes:
            target_dict = self.canvas_scenes[canvas_uuid]
        else:
            target_dict = self.scenes
        return [{"sceneName": name} for name in target_dict.keys()]

    def request(self, request_type, request_data=None):
        data = dict(request_data or {})
        if request_type == "GetCanvasList":
            return {"canvases": self.get_canvas_list()}

        if request_type == "GetSceneList":
            canvas_uuid = data.get("canvasUuid")
            return {"scenes": self.get_scene_list(canvas_uuid=canvas_uuid)}

        if request_type == "GetSceneItemList":
            scene_name = data["sceneName"]
            canvas_uuid = data.get("canvasUuid")
            if canvas_uuid:
                self.calls.append(("GetSceneItemList", scene_name, canvas_uuid))
            else:
                self.calls.append(("GetSceneItemList", scene_name))
            if self.get_scene_item_list_hook is not None:
                try:
                    res = self.get_scene_item_list_hook(self, scene_name, canvas_uuid)
                except TypeError:
                    res = self.get_scene_item_list_hook(self, scene_name)
                if res is not None:
                    return res
            if canvas_uuid and canvas_uuid in self.canvas_scenes:
                target_dict = self.canvas_scenes[canvas_uuid]
            else:
                target_dict = self.scenes
            if scene_name not in target_dict:
                raise ObsWsError(
                    f"GetSceneItemList が失敗しました: code=600 No scene named {scene_name}",
                    code=600,
                    comment="No scene",
                )
            return {"sceneItems": copy.deepcopy(target_dict[scene_name])}

        if request_type == "SetSceneItemEnabled":
            scene_name = data["sceneName"]
            item_id = data["sceneItemId"]
            enabled = bool(data["sceneItemEnabled"])
            canvas_uuid = data.get("canvasUuid")
            if canvas_uuid:
                self.calls.append(("SetSceneItemEnabled", scene_name, item_id, enabled, canvas_uuid))
            else:
                self.calls.append(("SetSceneItemEnabled", scene_name, item_id, enabled))
            if self.set_scene_item_enabled_hook is not None:
                try:
                    self.set_scene_item_enabled_hook(self, scene_name, item_id, enabled, canvas_uuid)
                except TypeError:
                    self.set_scene_item_enabled_hook(self, scene_name, item_id, enabled)
            if canvas_uuid and canvas_uuid in self.canvas_scenes:
                target_dict = self.canvas_scenes[canvas_uuid]
            else:
                target_dict = self.scenes
            if scene_name not in target_dict:
                raise ObsWsError(
                    f"SetSceneItemEnabled が失敗しました: code=600 No scene named {scene_name}",
                    code=600,
                    comment="No scene",
                )
            for item in target_dict[scene_name]:
                if item["sceneItemId"] == item_id:
                    item["sceneItemEnabled"] = enabled
                    return {}
            raise ObsWsError(
                f"SetSceneItemEnabled が失敗しました: code=600 No scene item {item_id}",
                code=600,
                comment="No scene item",
            )

        raise AssertionError(f"Unexpected request: {request_type} {data}")


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestSubtitleMuteLinkConfigUnit(unittest.TestCase):
    def test_parse_bool_flag_accepts_bool_and_strict_strings_only(self):
        self.assertTrue(sml.parse_bool_flag(True))
        self.assertFalse(sml.parse_bool_flag(False))
        self.assertTrue(sml.parse_bool_flag("true"))
        self.assertTrue(sml.parse_bool_flag("  TRUE "))
        self.assertFalse(sml.parse_bool_flag("false"))
        self.assertFalse(sml.parse_bool_flag(" FALSE "))
        for invalid in ("yes", "1", "on", "", "true!", 1, 0, None, [], {}):
            self.assertFalse(sml.parse_bool_flag(invalid), f"expected False for {invalid!r}")

    def test_extract_and_active_checks_noop_conditions(self):
        inactive_cases = [
            None,
            {},
            {"subtitle_mute_link": None},
            {"subtitle_mute_link": {"enabled": False, "input_name": "Mic A", "source_names": ["Subtitle Overlay"]}},
            {"subtitle_mute_link": {"enabled": "false", "input_name": "Mic A", "source_names": ["Subtitle Overlay"]}},
            {"subtitle_mute_link": {"enabled": "yes", "input_name": "Mic A", "source_names": ["Subtitle Overlay"]}},
            {"subtitle_mute_link": {"enabled": True, "input_name": "   ", "source_names": ["Subtitle Overlay"]}},
            {"subtitle_mute_link": {"enabled": True, "input_name": "Mic A", "source_names": []}},
            {"subtitle_mute_link": {"enabled": True, "input_name": "Mic A", "source_names": ["", "  ", 123]}},
        ]
        for cfg in inactive_cases:
            extracted = sml.extract_link_config(cfg)
            self.assertFalse(sml.is_link_config_active(extracted), f"should be inactive: {cfg!r}")

        active = sml.extract_link_config({
            "subtitle_mute_link": {
                "enabled": "true",
                "input_name": " Mic A ",
                "source_names": [" Subtitle Overlay ", "Subtitle Backup", "Subtitle Overlay", ""],
            }
        })
        self.assertTrue(sml.is_link_config_active(active))
        self.assertEqual(active, {
            "enabled": True,
            "input_name": "Mic A",
            "source_names": ["Subtitle Overlay", "Subtitle Backup"],
        })


class TestSubtitleMuteLinkSemanticsUnit(unittest.TestCase):
    def test_mute_records_state_before_hiding_and_preserves_manual_hidden_and_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                        {"sceneItemId": 2, "sourceName": "Subtitle Backup", "sceneItemEnabled": False},
                        {"sceneItemId": 3, "sourceName": "Other Source", "sceneItemEnabled": True},
                    ],
                    "Scene 2": [
                        {"sceneItemId": 10, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                    ],
                },
                mutes={"Mic A": True},
            )

            recorded_before_first_hide = []

            def _check_state_before_hide(_client, scene_name, item_id, enabled):
                if not enabled and not recorded_before_first_hide:
                    recorded_before_first_hide.extend(sml.load_hidden_state(state_path))

            obs.set_scene_item_enabled_hook = _check_state_before_hide

            had_transient = sml.reconcile(
                obs, "Mic A", ("Subtitle Overlay", "Subtitle Backup"), state_path
            )
            self.assertFalse(had_transient)

            # SetSceneItemEnabled=False が呼ばれる前に状態ファイルへ記録済みであること
            self.assertEqual(
                recorded_before_first_hide,
                [
                    {"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"},
                    {"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"},
                ],
            )
            # 表示中の対象ソースだけ非表示になり、手動非表示・対象外は不変
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertFalse(obs.scenes["Scene 1"][1]["sceneItemEnabled"])
            self.assertTrue(obs.scenes["Scene 1"][2]["sceneItemEnabled"])
            self.assertFalse(obs.scenes["Scene 2"][0]["sceneItemEnabled"])
            self.assertEqual(
                [c for c in obs.calls if c[0] == "SetSceneItemEnabled"],
                [
                    ("SetSceneItemEnabled", "Scene 1", 1, False),
                    ("SetSceneItemEnabled", "Scene 2", 10, False),
                ],
            )

            # ミュート解除で連動が隠した 2 件だけが復帰し、手動非表示や対象外は触らない
            obs.calls.clear()
            obs.mutes["Mic A"] = False
            had_transient = sml.reconcile(
                obs, "Mic A", ("Subtitle Overlay", "Subtitle Backup"), state_path
            )
            self.assertFalse(had_transient)
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertFalse(obs.scenes["Scene 1"][1]["sceneItemEnabled"])
            self.assertTrue(obs.scenes["Scene 1"][2]["sceneItemEnabled"])
            self.assertTrue(obs.scenes["Scene 2"][0]["sceneItemEnabled"])
            self.assertEqual(
                [c for c in obs.calls if c[0] == "SetSceneItemEnabled"],
                [
                    ("SetSceneItemEnabled", "Scene 1", 1, True),
                    ("SetSceneItemEnabled", "Scene 2", 10, True),
                ],
            )
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_unmute_discards_renamed_deleted_or_missing_scene_items_without_touching(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            sml.save_hidden_state(
                [
                    {"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"},
                    {"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"},
                    {"scene_name": "Deleted Scene", "scene_item_id": 20, "source_name": "Subtitle Overlay"},
                ],
                state_path,
            )
            obs = _FakeObsClient(
                scenes={
                    # item_id=1 は別ソースに差し替わっている
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Different Source", "sceneItemEnabled": False},
                    ],
                    # item_id=10 は削除済み
                    "Scene 2": [],
                },
                mutes={"Mic A": False},
            )

            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(had_transient)
            self.assertEqual([c for c in obs.calls if c[0] == "SetSceneItemEnabled"], [])
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_transient_not_ready_on_restore_keeps_record_and_retries_on_later_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            sml.save_hidden_state(
                [
                    {"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"},
                    {"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"},
                ],
                state_path,
            )
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                    "Scene 2": [
                        {"sceneItemId": 10, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                },
                mutes={"Mic A": False},
            )

            # 1回目: Scene 2 の SetSceneItemEnabled で OBS NotReady (code=207)
            fail_scene2 = {"active": True}

            def _fail_once(_client, scene_name, item_id, enabled):
                if fail_scene2["active"] and scene_name == "Scene 2" and enabled:
                    raise ObsWsError(
                        "SetSceneItemEnabled が失敗しました: code=207 OBS is not ready",
                        code=207,
                        comment="OBS is not ready",
                    )

            obs.set_scene_item_enabled_hook = _fail_once

            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertTrue(had_transient)
            # Scene 1 は復帰して記録から消え、一時エラーの Scene 2 だけ記録が残る
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertFalse(obs.scenes["Scene 2"][0]["sceneItemEnabled"])
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"}],
            )

            # 2回目: NotReady 解消後の reconcile で Scene 2 が復帰し記録もクリアされる
            fail_scene2["active"] = False
            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(had_transient)
            self.assertTrue(obs.scenes["Scene 2"][0]["sceneItemEnabled"])
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_transient_get_scene_item_list_not_ready_keeps_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            sml.save_hidden_state(
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
                state_path,
            )
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                },
                mutes={"Mic A": False},
            )
            obs.get_scene_item_list_hook = lambda _c, _s: (_ for _ in ()).throw(
                ObsWsError("GetSceneItemList が失敗しました: code=207 NotReady", code=207)
            )

            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertTrue(had_transient)
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
            )

    def test_transport_error_during_restore_preserves_remaining_state_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            sml.save_hidden_state(
                [
                    {"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"},
                    {"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"},
                ],
                state_path,
            )
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                    "Scene 2": [
                        {"sceneItemId": 10, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                },
                mutes={"Mic A": False},
            )

            def _disconnect_on_scene2(_client, scene_name, _item_id, _enabled):
                if scene_name == "Scene 2":
                    raise ObsWsError("WebSocket が切断されました")

            obs.set_scene_item_enabled_hook = _disconnect_on_scene2

            with self.assertRaises(ObsWsError):
                sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)

            # 切断前に完了した Scene 1 はクリアされ、未完了の Scene 2 が残る
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 2", "scene_item_id": 10, "source_name": "Subtitle Overlay"}],
            )

    def test_restart_while_muted_keeps_hidden_and_restores_after_unmute(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                    ],
                },
                mutes={"Mic A": True},
            )

            # ミュート適用
            sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(len(sml.load_hidden_state(state_path)), 1)

            # ミュート状態のままサーバー再起動を模擬（同じ state_path・同じ OBS 状態で再 reconcile）
            sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
            )

            # 再起動後にミュート解除 -> 復帰
            obs.mutes["Mic A"] = False
            sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_reconcile_rechecks_get_input_mute_and_catches_mid_processing_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            sml.save_hidden_state(
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
                state_path,
            )
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                },
                mutes={"Mic A": False},
            )

            flipped = {"done": False}

            def _flip_to_muted_during_restore(client, _scene, _item_id, enabled):
                if enabled and not flipped["done"]:
                    flipped["done"] = True
                    # 復帰処理の途中でミュートへ反転したケース
                    client.mutes["Mic A"] = True

            obs.set_scene_item_enabled_hook = _flip_to_muted_during_restore

            sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            # 再確認でミュート反転を検知し、再度非表示＋記録されていること
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
            )

    def test_reconcile_catches_unmute_flip_during_hide(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                    ],
                },
                mutes={"Mic A": True},
            )
            flipped = {"done": False}

            def _flip_to_unmuted_during_hide(client, _scene, _item_id, enabled):
                if not enabled and not flipped["done"]:
                    flipped["done"] = True
                    client.mutes["Mic A"] = False

            obs.set_scene_item_enabled_hook = _flip_to_unmuted_during_hide

            sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            # 非表示処理中のミュート解除を GetInputMute 再確認で検知して復帰済みであること
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_multi_canvas_hide_and_restore_with_canvas_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            canvas_main = "canvas-main-uuid"
            canvas_vert = "canvas-vertical-uuid"
            obs = _FakeObsClient(
                canvases=[
                    {"canvasUuid": canvas_main, "canvasName": "Main"},
                    {"canvasUuid": canvas_vert, "canvasName": "Vertical"},
                ],
                canvas_scenes={
                    canvas_main: {
                        "Main Scene": [
                            {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                            {"sceneItemId": 2, "sourceName": "Other", "sceneItemEnabled": True},
                        ],
                    },
                    canvas_vert: {
                        "Vertical Scene": [
                            {"sceneItemId": 10, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                            {"sceneItemId": 11, "sourceName": "Manual Hidden Subtitle", "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                        ],
                    },
                },
                mutes={"Mic A": True},
            )

            # ミュート時: メインと別キャンバス両方の表示中対象ソースが非表示になり、canvas_uuid 付きで記録
            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(had_transient)

            self.assertFalse(obs.canvas_scenes[canvas_main]["Main Scene"][0]["sceneItemEnabled"])
            self.assertTrue(obs.canvas_scenes[canvas_main]["Main Scene"][1]["sceneItemEnabled"])
            self.assertFalse(obs.canvas_scenes[canvas_vert]["Vertical Scene"][0]["sceneItemEnabled"])
            self.assertFalse(obs.canvas_scenes[canvas_vert]["Vertical Scene"][1]["sceneItemEnabled"])

            recorded = sml.load_hidden_state(state_path)
            self.assertEqual(len(recorded), 2)
            self.assertIn(
                {
                    "canvas_uuid": canvas_main,
                    "scene_name": "Main Scene",
                    "scene_item_id": 1,
                    "source_name": "Subtitle Overlay",
                },
                recorded,
            )
            self.assertIn(
                {
                    "canvas_uuid": canvas_vert,
                    "scene_name": "Vertical Scene",
                    "scene_item_id": 10,
                    "source_name": "Subtitle Overlay",
                },
                recorded,
            )

            # SetSceneItemEnabled 呼び出しに canvasUuid が渡されていることを確認
            hide_calls = [c for c in obs.calls if c[0] == "SetSceneItemEnabled"]
            self.assertIn(("SetSceneItemEnabled", "Main Scene", 1, False, canvas_main), hide_calls)
            self.assertIn(("SetSceneItemEnabled", "Vertical Scene", 10, False, canvas_vert), hide_calls)

            # ミュート解除時: 両方のキャンバスで正しく復帰し、SetSceneItemEnabled に canvasUuid が渡される
            obs.calls.clear()
            obs.mutes["Mic A"] = False
            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(had_transient)

            self.assertTrue(obs.canvas_scenes[canvas_main]["Main Scene"][0]["sceneItemEnabled"])
            self.assertTrue(obs.canvas_scenes[canvas_vert]["Vertical Scene"][0]["sceneItemEnabled"])
            self.assertFalse(obs.canvas_scenes[canvas_vert]["Vertical Scene"][1]["sceneItemEnabled"])

            restore_calls = [c for c in obs.calls if c[0] == "SetSceneItemEnabled"]
            self.assertIn(("SetSceneItemEnabled", "Main Scene", 1, True, canvas_main), restore_calls)
            self.assertIn(("SetSceneItemEnabled", "Vertical Scene", 10, True, canvas_vert), restore_calls)
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_legacy_record_without_canvas_uuid_restores_to_main_canvas(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            # canvas_uuid がない旧形式の状態ファイルを直接書き出す
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": 1,
                    "hidden_items": [
                        {"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}
                    ],
                }, f)

            loaded = sml.load_hidden_state(state_path)
            self.assertEqual(len(loaded), 1)
            self.assertNotIn("canvas_uuid", loaded[0])

            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                    ],
                },
                mutes={"Mic A": False},
            )

            # 旧記録の復帰: canvasUuid を渡さない従来呼び出しで復帰できること
            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            self.assertFalse(had_transient)
            self.assertTrue(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(
                [c for c in obs.calls if c[0] == "SetSceneItemEnabled"],
                [("SetSceneItemEnabled", "Scene 1", 1, True)],
            )
            self.assertEqual(sml.load_hidden_state(state_path), [])

    def test_get_canvas_list_unsupported_fallback_to_main_canvas_without_retry_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "subtitle_mute_link_state.json")
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                    ],
                },
                mutes={"Mic A": True},
            )
            # GetCanvasList は UnknownRequestType (code=204) を送出するよう設定
            canvas_call_count = {"count": 0}

            def _canvas_error(_client):
                canvas_call_count["count"] += 1
                raise ObsWsError("GetCanvasList が失敗しました: code=204 UnknownRequestType", code=204)

            obs.get_canvas_list_hook = _canvas_error

            had_transient = sml.reconcile(obs, "Mic A", ("Subtitle Overlay",), state_path)
            # 未対応は恒久エラーとして扱われ、transient=False でメインキャンバスのみ処理
            self.assertFalse(had_transient)
            self.assertFalse(obs.scenes["Scene 1"][0]["sceneItemEnabled"])
            self.assertEqual(
                sml.load_hidden_state(state_path),
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
            )
            # メインキャンバスへの操作は canvasUuid なしで発行される
            self.assertEqual(
                [c for c in obs.calls if c[0] == "SetSceneItemEnabled"],
                [("SetSceneItemEnabled", "Scene 1", 1, False)],
            )
            self.assertEqual(canvas_call_count["count"], 1)


class TestSubtitleMuteLinkServiceUnit(unittest.TestCase):
    def test_default_obs_client_factory_subscribes_to_input_events(self):
        with (
            mock.patch.object(sml, "load_twitch_config", return_value={"obs_password": "pw-test"}),
            mock.patch.object(sml, "ObsWebSocketClient") as mock_ws,
        ):
            sml._default_obs_client_factory()
            mock_ws.assert_called_once_with(
                host=sml.OBS_WS_UPSTREAM_HOST,
                port=sml.OBS_WS_UPSTREAM_PORT,
                password="pw-test",
                timeout=4.0,
                event_subscriptions=sml.EVENT_SUBSCRIPTION_INPUTS,
            )

    def test_noop_when_disabled_or_incomplete_never_connects(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            created = []
            svc = sml.SubtitleMuteLinkService(
                config_loader=lambda: {
                    "subtitle_mute_link": {"enabled": False, "input_name": "Mic A", "source_names": ["Subtitle Overlay"]}
                },
                client_factory=lambda: created.append(True) or _FakeObsClient(),
                state_path=state_path,
            )
            self.assertFalse(svc.start())
            self.assertFalse(svc.is_running())
            self.assertEqual(created, [])
            svc.stop()

    def test_loader_failure_logs_and_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")

            def _broken_loader():
                raise RuntimeError("simulated config failure")

            svc = sml.SubtitleMuteLinkService(
                config_loader=_broken_loader,
                state_path=state_path,
            )
            self.assertFalse(svc.start())
            self.assertFalse(svc.is_running())
            svc.stop()

    def test_live_events_and_reconfigure_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            cfg_holder = {
                "value": {
                    "subtitle_mute_link": {
                        "enabled": False,
                        "input_name": "",
                        "source_names": [],
                    }
                }
            }
            clients = []

            def _factory():
                c = _FakeObsClient(
                    scenes={
                        "Scene 1": [
                            {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                        ],
                    },
                    mutes={"Mic A": False, "Mic B": False},
                )
                clients.append(c)
                return c

            svc = sml.SubtitleMuteLinkService(
                config_loader=lambda: cfg_holder["value"],
                client_factory=_factory,
                state_path=state_path,
                reconnect_interval=0.05,
                poll_slice=0.05,
                retry_reconcile_interval=0.05,
            )
            try:
                # 初期は無効 -> no-op
                self.assertFalse(svc.start())
                self.assertEqual(len(clients), 0)

                # 設定変更して reconfigure -> 監視開始
                cfg_holder["value"] = {
                    "subtitle_mute_link": {
                        "enabled": True,
                        "input_name": "Mic A",
                        "source_names": ["Subtitle Overlay"],
                    }
                }
                self.assertTrue(svc.reconfigure())
                self.assertTrue(_wait_until(lambda: len(clients) == 1 and clients[0].reconcile_done_event.is_set()))
                c1 = clients[0]

                # 対象外マイクのイベントは無視
                c1.mutes["Mic B"] = True
                c1.push_event("InputMuteStateChanged", {"inputName": "Mic B", "inputMuted": True})
                time.sleep(0.1)
                self.assertTrue(c1.scenes["Scene 1"][0]["sceneItemEnabled"])

                # 対象マイクのミュートイベントで非表示
                c1.mutes["Mic A"] = True
                c1.push_event("InputMuteStateChanged", {"inputName": "Mic A", "inputMuted": True})
                self.assertTrue(_wait_until(lambda: not c1.scenes["Scene 1"][0]["sceneItemEnabled"]))

                # 解除イベントで復帰
                c1.mutes["Mic A"] = False
                c1.push_event("InputMuteStateChanged", {"inputName": "Mic A", "inputMuted": False})
                self.assertTrue(_wait_until(lambda: c1.scenes["Scene 1"][0]["sceneItemEnabled"]))
                self.assertEqual(sml.load_hidden_state(state_path), [])

                # 対象マイクを変更して reconfigure -> 旧接続を閉じて再構成
                cfg_holder["value"] = {
                    "subtitle_mute_link": {
                        "enabled": True,
                        "input_name": "Mic B",
                        "source_names": ["Subtitle Overlay"],
                    }
                }
                self.assertTrue(svc.reconfigure())
                self.assertTrue(c1.closed)
                self.assertTrue(_wait_until(lambda: len(clients) == 2 and clients[1].reconcile_done_event.is_set()))

                # 無効化して reconfigure -> 停止
                cfg_holder["value"] = {
                    "subtitle_mute_link": {
                        "enabled": False,
                        "input_name": "Mic B",
                        "source_names": ["Subtitle Overlay"],
                    }
                }
                self.assertFalse(svc.reconfigure())
                self.assertFalse(svc.is_running())
                self.assertTrue(clients[1].closed)
            finally:
                svc.stop()

    def test_reconnect_and_not_ready_auto_retry_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            sml.save_hidden_state(
                [{"scene_name": "Scene 1", "scene_item_id": 1, "source_name": "Subtitle Overlay"}],
                state_path,
            )
            shared_scenes = {
                "Scene 1": [
                    {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": False},
                ],
            }
            attempt = {"n": 0}
            not_ready_once = {"active": True}
            clients = []

            def _factory():
                attempt["n"] += 1
                c = _FakeObsClient(scenes=shared_scenes, mutes={"Mic A": False})
                # 1本目の接続は connect で失敗させ、再接続を検証する
                if attempt["n"] == 1:
                    c.connect_hook = lambda _c: (_ for _ in ()).throw(ObsWsError("接続失敗"))
                else:
                    # 2本目の接続の初回復帰だけ NotReady (207) にし、後の自動再試行で復帰させる
                    def _hook(_c, _scene, _item_id, enabled):
                        if enabled and not_ready_once["active"]:
                            not_ready_once["active"] = False
                            raise ObsWsError("SetSceneItemEnabled が失敗しました: code=207 NotReady", code=207)

                    c.set_scene_item_enabled_hook = _hook
                clients.append(c)
                return c

            svc = sml.SubtitleMuteLinkService(
                config_loader=lambda: {
                    "subtitle_mute_link": {
                        "enabled": True,
                        "input_name": "Mic A",
                        "source_names": ["Subtitle Overlay"],
                    }
                },
                client_factory=_factory,
                state_path=state_path,
                reconnect_interval=0.05,
                poll_slice=0.03,
                retry_reconcile_interval=0.05,
            )
            try:
                self.assertTrue(svc.start())
                self.assertTrue(
                    _wait_until(
                        lambda: len(clients) >= 2 and clients[1].scenes["Scene 1"][0]["sceneItemEnabled"],
                        timeout=3.0,
                    )
                )
                self.assertEqual(sml.load_hidden_state(state_path), [])
            finally:
                svc.stop()

    def test_stop_right_after_connect_skips_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            entered_connect = threading.Event()
            release_connect = threading.Event()
            obs = _FakeObsClient(
                scenes={
                    "Scene 1": [
                        {"sceneItemId": 1, "sourceName": "Subtitle Overlay", "sceneItemEnabled": True},
                    ],
                },
                mutes={"Mic A": True},
            )

            def _slow_connect(_client):
                entered_connect.set()
                release_connect.wait(timeout=2.0)

            obs.connect_hook = _slow_connect

            svc = sml.SubtitleMuteLinkService(
                config_loader=lambda: {
                    "subtitle_mute_link": {
                        "enabled": True,
                        "input_name": "Mic A",
                        "source_names": ["Subtitle Overlay"],
                    }
                },
                client_factory=lambda: obs,
                state_path=state_path,
                reconnect_interval=0.05,
                poll_slice=0.05,
            )
            try:
                self.assertTrue(svc.start())
                self.assertTrue(entered_connect.wait(timeout=2.0))

                stopper = threading.Thread(target=svc.stop)
                stopper.start()
                # connect() が戻る直前に stop_event が立っている状態を再現
                time.sleep(0.05)
                release_connect.set()
                stopper.join(timeout=3.0)

                self.assertFalse(svc.is_running())
                # 接続直後の停止チェックにより reconcile (GetInputMute) は呼ばれない
                self.assertEqual(obs.calls, [])
            finally:
                release_connect.set()
                svc.stop()


class TestSubtitleMuteLinkHttpIntegration(unittest.TestCase):
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

    def test_post_voice_command_config_saves_subtitle_mute_link_and_triggers_reconfigure(self):
        reconfig_calls = []
        payload = {
            "subtitle_mute_link": {
                "enabled": True,
                "input_name": "Mic A",
                "source_names": ["Subtitle Overlay", "Subtitle Backup"],
            }
        }
        with (
            mock.patch.object(voice_command, "VOICE_COMMAND_CONFIG_FILE", self.cfg_path),
            mock.patch.dict(voice_command._CONFIG_CACHE, {"key": None, "value": None}),
            mock.patch.object(
                server_handler,
                "_reconfigure_subtitle_mute_link",
                side_effect=lambda: reconfig_calls.append(True),
            ),
        ):
            st, body = _http_request(
                f"{self.base_url}/api/voice-command/config",
                method="POST",
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(st, 200)
            resp = json.loads(body.decode("utf-8"))
            self.assertTrue(resp["ok"])
            self.assertEqual(
                resp["config"]["subtitle_mute_link"],
                {
                    "enabled": True,
                    "input_name": "Mic A",
                    "source_names": ["Subtitle Overlay", "Subtitle Backup"],
                },
            )
            self.assertEqual(len(reconfig_calls), 1)

            st_get, body_get = _http_request(f"{self.base_url}/api/voice-command/config", method="GET")
            self.assertEqual(st_get, 200)
            cfg = json.loads(body_get.decode("utf-8"))
            self.assertEqual(cfg["subtitle_mute_link"], resp["config"]["subtitle_mute_link"])

    def test_state_file_is_blocked_from_http_static_serving(self):
        for path in ("/subtitle_mute_link_state.json", "/subtitle_mute_link_state.json.tmp.1"):
            st_get, _ = _http_request(f"{self.base_url}{path}", method="GET")
            self.assertEqual(st_get, 403, f"GET {path} should be 403")
            st_head, _ = _http_request(f"{self.base_url}{path}", method="HEAD")
            self.assertEqual(st_head, 403, f"HEAD {path} should be 403")


if __name__ == "__main__":
    unittest.main(verbosity=2)
