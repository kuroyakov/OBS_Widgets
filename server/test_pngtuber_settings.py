#!/usr/bin/env python3
"""
PngTuber 設定保存・画像アップロード・OBS 入力一覧 API のテスト (Issue #5)
実行: python3 server/test_pngtuber_settings.py
"""

import io
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
from server import pngtuber_audio  # noqa: E402
from server.obs_ws import ObsWsError  # noqa: E402

# 最小の有効な 1x1 透過 PNG バイト列
MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _build_multipart_body(field_name, filename, content_bytes, content_type="image/png", boundary="----PngTuberTestBoundary7MA4YWxkTrZu0gW"):
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode("ascii"))
    buf.write(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    buf.write(content_bytes)
    buf.write(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", buf.getvalue()


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _FakeObsClient:
    def __init__(self, inputs=None, error=None):
        self._inputs = inputs if inputs is not None else []
        self._error = error

    def __enter__(self):
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, request_type, request_data=None):
        if self._error is not None:
            raise self._error
        if request_type == "GetInputList":
            return {"inputs": list(self._inputs)}
        return {}


class TestPngTuberMergeSaveUnit(unittest.TestCase):
    def test_merge_save_preserves_avatars_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            initial = {
                "_comment": "dummy comment",
                "custom_extension_flag": True,
                "obs_ws_host": "127.0.0.1",
                "obs_ws_port": 4455,
                "obs_ws_password": "dummy-secret-value",
                "images": {
                    "mouth_open_eye_open": "images/open_open.png",
                    "mouth_open_eye_closed": "images/open_closed.png",
                    "mouth_closed_eye_open": "images/closed_open.png",
                    "mouth_closed_eye_closed": "images/closed_closed.png",
                    "extra_meta": "keep-me",
                },
                "obs_input_name": "Old Mic",
                "open_threshold": 0.05,
                "close_threshold": 0.025,
                "avatars": {
                    "partner": {
                        "images": {
                            "mouth_open_eye_open": "images/partner/oo.png",
                        },
                        "obs_input_name": "Sub Mic",
                    }
                },
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(initial, f)

            saved = pngtuber_audio.save_pngtuber_config(
                {
                    "obs_input_name": "Main Mic",
                    "open_threshold": 0.08,
                    "close_threshold": 0.04,
                    "images": {"mouth_open_eye_open": "images/new_open.png"},
                    # 対象外のキーは保存 API 経由では無視されること
                    "avatars": {"partner": {"obs_input_name": "OVERWRITTEN"}},
                    "obs_ws_password": "OVERWRITTEN",
                    "injected_unknown": "should-not-appear",
                },
                config_path=cfg_path,
            )

            with open(cfg_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)

            self.assertEqual(on_disk, saved)
            self.assertEqual(on_disk["obs_input_name"], "Main Mic")
            self.assertEqual(on_disk["open_threshold"], 0.08)
            self.assertEqual(on_disk["close_threshold"], 0.04)
            # 更新スロットのみ差し替えられ、他スロットと images 内未知キーは維持
            self.assertEqual(on_disk["images"]["mouth_open_eye_open"], "images/new_open.png")
            self.assertEqual(on_disk["images"]["mouth_closed_eye_open"], "images/closed_open.png")
            self.assertEqual(on_disk["images"]["extra_meta"], "keep-me")
            # avatars・obs_ws_*・未知トップレベルキーが壊れないこと
            self.assertEqual(on_disk["avatars"], initial["avatars"])
            self.assertEqual(on_disk["obs_ws_password"], "dummy-secret-value")
            self.assertEqual(on_disk["_comment"], "dummy comment")
            self.assertTrue(on_disk["custom_extension_flag"])
            self.assertNotIn("injected_unknown", on_disk)
            # 一時ファイルが残っていないこと
            self.assertEqual(os.listdir(tmp), ["pngtuber_config.json"])

    def test_merge_save_creates_clean_file_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            saved = pngtuber_audio.save_pngtuber_config(
                {"obs_input_name": "", "open_threshold": 0.06, "close_threshold": 0.03},
                config_path=cfg_path,
            )
            self.assertTrue(os.path.isfile(cfg_path))
            self.assertEqual(saved["obs_input_name"], "")
            self.assertEqual(saved["open_threshold"], 0.06)
            self.assertEqual(saved["close_threshold"], 0.03)
            self.assertNotIn("avatars", saved)
            self.assertNotIn("obs_ws_password", saved)

    def test_merge_save_clamps_close_threshold_below_open_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            pngtuber_audio.save_pngtuber_config(
                {"open_threshold": 0.1, "close_threshold": 0.08},
                config_path=cfg_path,
            )
            # open_threshold だけ下げたとき既存 close_threshold も自動追従して整合性を保つ
            saved = pngtuber_audio.save_pngtuber_config(
                {"open_threshold": 0.04},
                config_path=cfg_path,
            )
            self.assertEqual(saved["open_threshold"], 0.04)
            self.assertEqual(saved["close_threshold"], 0.04)

    def test_merge_save_rejects_invalid_inputs_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config({"open_threshold": "not-a-number"}, config_path=cfg_path)
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config({"open_threshold": True}, config_path=cfg_path)
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config(
                    {"images": {"mouth_open_eye_open": "../secret.png"}},
                    config_path=cfg_path,
                )
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config(
                    {"images": {"mouth_open_eye_open": "images/bad.exe"}},
                    config_path=cfg_path,
                )
            self.assertFalse(os.path.exists(cfg_path))

            # 破損した既存 JSON は上書きせず拒否すること
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("{broken json")
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config({"open_threshold": 0.05}, config_path=cfg_path)
            with open(cfg_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "{broken json")


class TestPngTuberAvatarSaveUnit(unittest.TestCase):
    def _write_initial(self, cfg_path):
        initial = {
            "_comment": "dummy comment",
            "obs_input_name": "Main Mic",
            "open_threshold": 0.05,
            "close_threshold": 0.025,
            "avatars": {
                "alpha": {"obs_input_name": "Alpha Mic", "open_threshold": 0.07},
                "beta": {"obs_input_name": "Beta Mic", "custom_note": "keep-me"},
            },
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(initial, f)
        return initial

    def test_avatar_save_updates_only_target_avatar(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            initial = self._write_initial(cfg_path)

            saved = pngtuber_audio.save_pngtuber_config(
                {"obs_input_name": "Alpha Mic 2", "close_threshold": 0.03},
                config_path=cfg_path,
                avatar="alpha",
            )

            # 対象アバターだけが変わり、メイン・他アバター・未知キーは保持される
            self.assertEqual(saved["avatars"]["alpha"]["obs_input_name"], "Alpha Mic 2")
            self.assertEqual(saved["avatars"]["alpha"]["close_threshold"], 0.03)
            self.assertEqual(saved["avatars"]["alpha"]["open_threshold"], 0.07)
            self.assertEqual(saved["avatars"]["beta"], initial["avatars"]["beta"])
            self.assertEqual(saved["obs_input_name"], "Main Mic")
            self.assertEqual(saved["_comment"], "dummy comment")

    def test_avatar_save_creates_new_avatar_and_validates_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            self._write_initial(cfg_path)

            saved = pngtuber_audio.save_pngtuber_config(
                {"obs_input_name": "Gamma Mic"},
                config_path=cfg_path,
                avatar="gamma-1",
            )
            self.assertEqual(saved["avatars"]["gamma-1"]["obs_input_name"], "Gamma Mic")
            self.assertEqual(saved["avatars"]["alpha"]["obs_input_name"], "Alpha Mic")

            # 空更新で新規アバターを作成できること（UI の追加操作と同じペイロード）
            saved_empty = pngtuber_audio.save_pngtuber_config(
                {},
                config_path=cfg_path,
                avatar="empty-1",
            )
            self.assertEqual(saved_empty["avatars"]["empty-1"], {})

            # 末尾改行が正規表現にマッチしないこと（\Z 検証）
            self.assertIsNone(pngtuber_audio._AVATAR_NAME_PATTERN.match("gamma-1\n"))

            for bad in ("", "  ", "default", "Default", "DEFAULT", "../evil", "a b", "a/b", "x" * 65, 123):
                with self.assertRaises(ValueError, msg=f"avatar={bad!r}"):
                    pngtuber_audio.save_pngtuber_config(
                        {"obs_input_name": "Nope"}, config_path=cfg_path, avatar=bad
                    )
            with open(cfg_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertNotIn("../evil", on_disk["avatars"])
            self.assertNotIn("default", on_disk["avatars"])
            self.assertNotIn("Default", on_disk["avatars"])
            self.assertNotIn("DEFAULT", on_disk["avatars"])

            # 手書き JSON に default / Default や正規表現外（日本語等）の既存キーがある場合:
            # - 予約名 "default"（大小無視）は既存キーにあっても拒否する
            # - 正規表現外の既存キー（日本語キー等）は更新許可し、未登録の日本語キーは拒否する
            on_disk["avatars"]["default"] = {"obs_input_name": "Legacy Default"}
            on_disk["avatars"]["Default"] = {"obs_input_name": "Legacy Default Upper"}
            on_disk["avatars"]["サブ"] = {"obs_input_name": "Old Sub Mic"}
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(on_disk, f, ensure_ascii=False)

            for reserved in ("default", "Default", "DEFAULT"):
                with self.assertRaises(ValueError, msg=f"reserved existing avatar={reserved!r}"):
                    pngtuber_audio.save_pngtuber_config(
                        {"obs_input_name": "Overwrite"}, config_path=cfg_path, avatar=reserved
                    )

            updated_jp = pngtuber_audio.save_pngtuber_config(
                {"obs_input_name": "New Sub Mic"}, config_path=cfg_path, avatar="サブ"
            )
            self.assertEqual(updated_jp["avatars"]["サブ"]["obs_input_name"], "New Sub Mic")

            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_config(
                    {"obs_input_name": "Unregistered"}, config_path=cfg_path, avatar="未登録"
                )

    def test_avatar_image_upload_updates_avatar_slot_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            initial = self._write_initial(cfg_path)
            initial["images"] = {
                "mouth_open_eye_open": "images/main_oo.png",
                "mouth_open_eye_closed": "images/main_oc.png",
                "mouth_closed_eye_open": "images/main_co.png",
                "mouth_closed_eye_closed": "images/main_cc.png",
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(initial, f)
            widget_dir = os.path.join(tmp, "PngTuber")
            os.makedirs(os.path.join(widget_dir, "images"), exist_ok=True)

            result = pngtuber_audio.save_pngtuber_image_upload(
                "mouth_open_eye_open",
                "alpha face.png",
                MINIMAL_PNG,
                pngtuber_dir=widget_dir,
                config_path=cfg_path,
                avatar="alpha",
            )
            self.assertEqual(result["path"], "images/alpha_face.png")
            with open(cfg_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            # images 未設定アバターの単一スロット更新では他スロットを展開せず、メインからの継承を保つ
            self.assertEqual(
                on_disk["avatars"]["alpha"]["images"],
                {"mouth_open_eye_open": "images/alpha_face.png"},
            )
            normalized = pngtuber_audio._normalize_pngtuber_bundle(on_disk)
            self.assertEqual(
                normalized["avatars"]["alpha"]["images"]["mouth_open_eye_open"],
                "images/alpha_face.png",
            )
            self.assertEqual(
                normalized["avatars"]["alpha"]["images"]["mouth_closed_eye_open"],
                "images/main_co.png",
            )
            self.assertEqual(on_disk["images"]["mouth_open_eye_open"], "images/main_oo.png")
            self.assertEqual(on_disk["avatars"]["beta"]["obs_input_name"], "Beta Mic")

            # 拒否されたアップロードでは画像ファイルが一切書かれないこと
            for bad_avatar, fname in (
                ("../evil", "rejected_evil.png"),
                ("Default", "rejected_default.png"),
                ("   ", "rejected_space.png"),
            ):
                with self.assertRaises(ValueError, msg=f"avatar={bad_avatar!r}"):
                    pngtuber_audio.save_pngtuber_image_upload(
                        "mouth_open_eye_open",
                        fname,
                        MINIMAL_PNG,
                        pngtuber_dir=widget_dir,
                        config_path=cfg_path,
                        avatar=bad_avatar,
                    )
                self.assertFalse(
                    os.path.exists(os.path.join(widget_dir, "images", fname)),
                    msg=f"rejected upload should not write {fname}",
                )


class TestPngTuberImageUploadUnit(unittest.TestCase):
    def test_sanitize_png_filename(self):
        self.assertEqual(
            pngtuber_audio.sanitize_png_filename("../../evil name (1).PNG", "mouth_open_eye_open"),
            "evil_name_1.png",
        )
        self.assertEqual(
            pngtuber_audio.sanitize_png_filename(r"folder\sub\avatar-01.png", "mouth_open_eye_open"),
            "avatar-01.png",
        )
        self.assertEqual(
            pngtuber_audio.sanitize_png_filename("立ち絵_口開.png", "mouth_open_eye_open"),
            "立ち絵_口開.png",
        )
        self.assertEqual(
            pngtuber_audio.sanitize_png_filename("CON.png", "mouth_open_eye_open"),
            "mouth_open_eye_open.png",
        )
        self.assertEqual(
            pngtuber_audio.sanitize_png_filename("", "mouth_closed_eye_open"),
            "mouth_closed_eye_open.png",
        )
        with self.assertRaises(ValueError):
            pngtuber_audio.sanitize_png_filename("photo.jpg", "mouth_open_eye_open")
        with self.assertRaises(ValueError):
            pngtuber_audio.sanitize_png_filename("vector.svg", "mouth_open_eye_open")

    def test_save_pngtuber_image_upload_writes_png_and_updates_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget_dir = os.path.join(tmp, "PngTuber")
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"avatars": {"guest": {"obs_input_name": "Guest Mic"}}}, f)

            res = pngtuber_audio.save_pngtuber_image_upload(
                "mouth_open_eye_open",
                "../my avatar!.png",
                MINIMAL_PNG,
                pngtuber_dir=widget_dir,
                config_path=cfg_path,
            )
            self.assertEqual(res["filename"], "my_avatar.png")
            self.assertEqual(res["path"], "images/my_avatar.png")

            saved_img = os.path.join(widget_dir, "images", "my_avatar.png")
            self.assertTrue(os.path.isfile(saved_img))
            with open(saved_img, "rb") as f:
                self.assertEqual(f.read(), MINIMAL_PNG)

            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertEqual(cfg["images"]["mouth_open_eye_open"], "images/my_avatar.png")
            self.assertEqual(cfg["avatars"]["guest"]["obs_input_name"], "Guest Mic")

            # list_pngtuber_images でも検出され、隠しファイルや非画像は除外されること
            with open(os.path.join(widget_dir, "images", ".hidden.png"), "wb") as f:
                f.write(MINIMAL_PNG)
            with open(os.path.join(widget_dir, "images", "notes.txt"), "w", encoding="utf-8") as f:
                f.write("ignore")
            self.assertEqual(
                pngtuber_audio.list_pngtuber_images(pngtuber_dir=widget_dir),
                ["images/my_avatar.png"],
            )

    def test_save_pngtuber_image_upload_rejects_non_png_signature_and_bad_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget_dir = os.path.join(tmp, "PngTuber")
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_image_upload(
                    "mouth_open_eye_open",
                    "fake.png",
                    b"not a png file",
                    pngtuber_dir=widget_dir,
                    config_path=cfg_path,
                )
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_image_upload(
                    "mouth_open_eye_open",
                    "pic.jpg",
                    MINIMAL_PNG,
                    pngtuber_dir=widget_dir,
                    config_path=cfg_path,
                )
            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_image_upload(
                    "unknown_slot",
                    "ok.png",
                    MINIMAL_PNG,
                    pngtuber_dir=widget_dir,
                    config_path=cfg_path,
                )
            self.assertFalse(os.path.exists(os.path.join(widget_dir, "images")))
            self.assertFalse(os.path.exists(cfg_path))


class TestObsAudioInputsUnit(unittest.TestCase):
    def test_list_obs_audio_inputs_filters_and_prioritizes_mics(self):
        sample_inputs = [
            {"inputName": "Background Image", "inputKind": "image_source"},
            {"inputName": "Desktop Audio", "inputKind": "coreaudio_output_capture"},
            {"inputName": "Main Mic", "inputKind": "coreaudio_input_capture"},
            {"inputName": "Title Text", "inputKind": "text_ft2_source"},
            {"inputName": "Sub Mic", "inputKind": "wasapi_input_capture"},
            {"inputName": "Main Mic", "inputKind": "coreaudio_input_capture"},
        ]
        monitor = pngtuber_audio.PngTuberAudioMonitor(
            config_loader=lambda: pngtuber_audio.DEFAULT_PNGTUBER_CONFIG,
            ws_settings_loader=lambda _cfg: {"host": "127.0.0.1", "port": 4455, "password": ""},
            client_factory=lambda **_kwargs: _FakeObsClient(inputs=sample_inputs),
        )
        res = monitor.list_obs_audio_inputs()
        self.assertTrue(res["connected"])
        self.assertEqual(res["inputs"], ["Main Mic", "Sub Mic", "Desktop Audio"])

    def test_list_obs_audio_inputs_handles_obs_disconnected(self):
        monitor = pngtuber_audio.PngTuberAudioMonitor(
            config_loader=lambda: pngtuber_audio.DEFAULT_PNGTUBER_CONFIG,
            ws_settings_loader=lambda _cfg: {"host": "127.0.0.1", "port": 4455, "password": ""},
            client_factory=lambda **_kwargs: _FakeObsClient(error=ObsWsError("connection refused")),
        )
        res = monitor.list_obs_audio_inputs()
        self.assertFalse(res["connected"])
        self.assertEqual(res["inputs"], [])
        self.assertIn("connection refused", res["error"])


class TestPngTuberSettingsHttp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self._tmp.name, "pngtuber_config.json")
        self.widget_dir = os.path.join(self._tmp.name, "PngTuber")
        os.makedirs(os.path.join(self.widget_dir, "images"), exist_ok=True)
        with open(os.path.join(self.widget_dir, "images", "existing.png"), "wb") as f:
            f.write(MINIMAL_PNG)

        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "images": {
                        "mouth_open_eye_open": "images/existing.png",
                        "mouth_open_eye_closed": "images/existing.png",
                        "mouth_closed_eye_open": "images/existing.png",
                        "mouth_closed_eye_closed": "images/existing.png",
                    },
                    "obs_input_name": "Initial Mic",
                    "open_threshold": 0.05,
                    "close_threshold": 0.025,
                    "avatars": {"partner": {"obs_input_name": "Partner Mic"}},
                    "custom_flag": "keep",
                },
                f,
            )

        self._patches = [
            mock.patch.object(pngtuber_audio, "PNGTUBER_CONFIG_FILE", self.cfg_path),
            mock.patch.object(pngtuber_audio, "PNGTUBER_DIR", self.widget_dir),
            mock.patch.object(
                server_handler,
                "_list_obs_audio_inputs",
                side_effect=lambda refresh=False: {
                    "connected": True,
                    "inputs": ["Dummy Mic A", "Dummy Mic B"],
                    "details": [],
                },
            ),
        ]
        for p in self._patches:
            p.start()
        pngtuber_audio._CONFIG_CACHE["key"] = None
        pngtuber_audio._CONFIG_CACHE["value"] = None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for p in reversed(self._patches):
            p.stop()
        pngtuber_audio._CONFIG_CACHE["key"] = None
        pngtuber_audio._CONFIG_CACHE["value"] = None
        self._tmp.cleanup()

    def test_settings_page_and_get_endpoints(self):
        status, body = _http_request(f"{self.base_url}/PngTuber/settings.html")
        self.assertEqual(status, 200)
        self.assertIn(b"PNGTuber", body)
        self.assertIn(b'id="mic-filter"', body)

        status, body = _http_request(f"{self.base_url}/api/pngtuber/inputs")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["connected"])
        self.assertEqual(data["inputs"], ["Dummy Mic A", "Dummy Mic B"])

        status, body = _http_request(f"{self.base_url}/api/pngtuber/images")
        self.assertEqual(status, 200)
        img_data = json.loads(body.decode("utf-8"))
        self.assertEqual(img_data["images"], ["images/existing.png"])

        # pngtuber_config.json や tmp ファイルへの直接静的アクセスは 403
        status, _ = _http_request(f"{self.base_url}/pngtuber_config.json")
        self.assertEqual(status, 403)
        status, _ = _http_request(f"{self.base_url}/pngtuber_config.json.tmp.123")
        self.assertEqual(status, 403)

    def test_post_config_merges_and_reflected_by_get(self):
        payload = json.dumps(
            {
                "obs_input_name": "Dummy Mic A",
                "open_threshold": 0.12,
                "close_threshold": 0.06,
                "avatars": {"partner": {"obs_input_name": "SHOULD_NOT_CHANGE"}},
            }
        ).encode("utf-8")
        status, body = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["status"], "saved")
        self.assertEqual(resp["config"]["obs_input_name"], "Dummy Mic A")
        self.assertEqual(resp["config"]["open_threshold"], 0.12)
        self.assertEqual(resp["config"]["close_threshold"], 0.06)

        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["avatars"]["partner"]["obs_input_name"], "Partner Mic")
        self.assertEqual(on_disk["custom_flag"], "keep")

        # 既存 GET /api/pngtuber/config が即座に更新値を返すこと
        status, body = _http_request(f"{self.base_url}/api/pngtuber/config")
        self.assertEqual(status, 200)
        cfg_resp = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg_resp["obs_input_name"], "Dummy Mic A")
        self.assertEqual(cfg_resp["open_threshold"], 0.12)
        self.assertEqual(cfg_resp["close_threshold"], 0.06)

        # 不正な JSON ボディは 400
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=b"{not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_post_config_for_avatar_and_get(self):
        payload = json.dumps(
            {"avatar": "partner", "obs_input_name": "Partner Mic 2", "open_threshold": 0.2}
        ).encode("utf-8")
        status, body = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["config"]["avatar"], "partner")
        self.assertEqual(resp["config"]["obs_input_name"], "Partner Mic 2")
        self.assertEqual(resp["config"]["open_threshold"], 0.2)
        self.assertIn("partner", resp["config"]["avatars"])

        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["obs_input_name"], "Initial Mic")
        self.assertEqual(on_disk["avatars"]["partner"]["obs_input_name"], "Partner Mic 2")
        self.assertEqual(on_disk["custom_flag"], "keep")

        status, body = _http_request(f"{self.base_url}/api/pngtuber/config?avatar=partner")
        self.assertEqual(status, 200)
        cfg_resp = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg_resp["obs_input_name"], "Partner Mic 2")

        status, body = _http_request(f"{self.base_url}/api/pngtuber/config")
        self.assertEqual(status, 200)
        cfg_resp = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg_resp["obs_input_name"], "Initial Mic")

        # 不正なアバター名（パストラバーサル、空白のみ、空文字、予約名 Default/DEFAULT）は 400 でメインを上書きしない
        for bad_avatar in ("../evil", "   ", "", "Default", "DEFAULT"):
            payload = json.dumps({"avatar": bad_avatar, "obs_input_name": "SHOULD_NOT_OVERWRITE"}).encode("utf-8")
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/config",
                method="POST",
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400, msg=f"avatar={bad_avatar!r}")

        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["obs_input_name"], "Initial Mic")
        self.assertNotIn("Default", on_disk["avatars"])
        self.assertNotIn("DEFAULT", on_disk["avatars"])

    def test_post_image_upload_for_avatar_and_validation(self):
        ct, mp_body = _build_multipart_body("file", "partner face.png", MINIMAL_PNG)
        status, body = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open?avatar=partner",
            method="POST",
            body=mp_body,
            headers={"Content-Type": ct},
        )
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["status"], "uploaded")
        self.assertEqual(resp["path"], "images/partner_face.png")
        self.assertEqual(resp["config"]["avatar"], "partner")
        self.assertTrue(os.path.isfile(os.path.join(self.widget_dir, "images", "partner_face.png")))

        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        # 対象アバターの該当スロットのみ更新され、メインや他スロット展開は起きないこと
        self.assertEqual(
            on_disk["avatars"]["partner"]["images"],
            {"mouth_open_eye_open": "images/partner_face.png"},
        )
        self.assertEqual(on_disk["images"]["mouth_open_eye_open"], "images/existing.png")

        # 不正な avatar クエリは 400 でファイルも書かれず、メインも上書きされないこと
        for qs, fname in (
            ("avatar=../evil", "evil_upload.png"),
            ("avatar=%20%20", "space_upload.png"),
            ("avatar=", "empty_upload.png"),
            ("avatar=Default", "default_upload.png"),
        ):
            bad_ct, bad_body = _build_multipart_body("file", fname, MINIMAL_PNG)
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open?{qs}",
                method="POST",
                body=bad_body,
                headers={"Content-Type": bad_ct},
            )
            self.assertEqual(status, 400, msg=f"qs={qs!r}")
            self.assertFalse(
                os.path.exists(os.path.join(self.widget_dir, "images", fname)),
                msg=f"rejected HTTP upload should not write {fname}",
            )

        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["images"]["mouth_open_eye_open"], "images/existing.png")

    def test_post_image_upload_multipart_and_validation(self):
        ct, mp_body = _build_multipart_body("file", "../uploaded face.png", MINIMAL_PNG)
        status, body = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open",
            method="POST",
            body=mp_body,
            headers={"Content-Type": ct},
        )
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["status"], "uploaded")
        self.assertEqual(resp["filename"], "uploaded_face.png")
        self.assertEqual(resp["path"], "images/uploaded_face.png")

        # 保存先と pngtuber_config.json のマージ結果を検証
        self.assertTrue(os.path.isfile(os.path.join(self.widget_dir, "images", "uploaded_face.png")))
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["images"]["mouth_open_eye_open"], "images/uploaded_face.png")
        self.assertEqual(on_disk["avatars"]["partner"]["obs_input_name"], "Partner Mic")

        # 生 image/png アップロード (Content-Type: image/png) も受理すること
        status, raw_resp_body = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_closed_eye_closed?filename=raw_face.png",
            method="POST",
            body=MINIMAL_PNG,
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(status, 200)
        raw_resp = json.loads(raw_resp_body.decode("utf-8"))
        self.assertEqual(raw_resp["path"], "images/raw_face.png")
        self.assertTrue(os.path.isfile(os.path.join(self.widget_dir, "images", "raw_face.png")))

        # 非 .png や壊れたバイト列は 400 かつファイル未作成
        ct_jpg, jpg_body = _build_multipart_body("file", "photo.jpg", MINIMAL_PNG, content_type="image/jpeg")
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open",
            method="POST",
            body=jpg_body,
            headers={"Content-Type": ct_jpg},
        )
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.widget_dir, "images", "photo.jpg")))

        ct_fake, fake_body = _build_multipart_body("file", "fake.png", b"hello world")
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open",
            method="POST",
            body=fake_body,
            headers={"Content-Type": ct_fake},
        )
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.widget_dir, "images", "fake.png")))

        ct_slot, slot_body = _build_multipart_body("file", "bad_slot_file.png", MINIMAL_PNG)
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/not_a_slot",
            method="POST",
            body=slot_body,
            headers={"Content-Type": ct_slot},
        )
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.widget_dir, "images", "bad_slot_file.png")))

    def test_post_endpoints_require_token_for_non_local_clients(self):
        fake_token = "dummy-test-widget-token"
        with mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False), \
             mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token), \
             mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token):
            # トークンなし -> 401
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/config",
                method="POST",
                body=json.dumps({"open_threshold": 0.09}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 401)

            ct, mp_body = _build_multipart_body("file", "tok.png", MINIMAL_PNG)
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/image/mouth_closed_eye_open",
                method="POST",
                body=mp_body,
                headers={"Content-Type": ct},
            )
            self.assertEqual(status, 401)

            # 有効なトークン付き -> 200
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/config",
                method="POST",
                body=json.dumps({"open_threshold": 0.09}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Widget-Token": fake_token},
            )
            self.assertEqual(status, 200)

            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/image/mouth_closed_eye_open",
                method="POST",
                body=mp_body,
                headers={"Content-Type": ct, "X-Widget-Token": fake_token},
            )
            self.assertEqual(status, 200)

    def test_get_inputs_requires_local_or_token(self):
        fake_token = "dummy-test-widget-token"
        # ループバック接続からはトークンなしでも 200
        status_local, _ = _http_request(f"{self.base_url}/api/pngtuber/inputs")
        self.assertEqual(status_local, 200)

        with mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False), \
             mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token), \
             mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token):
            # 非ループバックかつトークンなし -> 401
            status_denied, _ = _http_request(f"{self.base_url}/api/pngtuber/inputs")
            self.assertEqual(status_denied, 401)

            # 有効なトークン付き -> 200
            status_token, _ = _http_request(
                f"{self.base_url}/api/pngtuber/inputs",
                headers={"X-Widget-Token": fake_token},
            )
            self.assertEqual(status_token, 200)

    def test_post_config_crosstalk_margin_db_validation(self):
        headers = {"Content-Type": "application/json"}
        status_ok, raw_ok = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=json.dumps({"crosstalk_margin_db": 6.0}).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(status_ok, 200)
        self.assertEqual(json.loads(raw_ok.decode("utf-8"))["config"]["crosstalk_margin_db"], 6.0)

        status_get, raw_get = _http_request(f"{self.base_url}/api/pngtuber/config")
        self.assertEqual(status_get, 200)
        self.assertEqual(json.loads(raw_get.decode("utf-8"))["crosstalk_margin_db"], 6.0)

        for out_of_range, expected in ((-1.0, 0.0), (24.5, 24.0)):
            status_clamp, raw_clamp = _http_request(
                f"{self.base_url}/api/pngtuber/config",
                method="POST",
                body=json.dumps({"crosstalk_margin_db": out_of_range}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(status_clamp, 200)
            self.assertEqual(json.loads(raw_clamp.decode("utf-8"))["config"]["crosstalk_margin_db"], expected)

        for invalid_val in ("abc", True):
            status_bad, _ = _http_request(
                f"{self.base_url}/api/pngtuber/config",
                method="POST",
                body=json.dumps({"crosstalk_margin_db": invalid_val}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(status_bad, 400, f"expected 400 for {invalid_val!r}")

        status_reset, raw_reset = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=json.dumps({"crosstalk_margin_db": 0.0}).encode("utf-8"),
            headers=headers,
        )
        self.assertEqual(status_reset, 200)
        self.assertEqual(json.loads(raw_reset.decode("utf-8"))["config"]["crosstalk_margin_db"], 0.0)


class PngTuberCrosstalkSuppressionTest(unittest.TestCase):
    def _make_monitor_and_bundle(self, default_cfg, guest_cfg):
        bundle = {
            "default": default_cfg,
            "avatars": {"guest": guest_cfg},
        }
        monitor = pngtuber_audio.PngTuberAudioMonitor(config_loader=lambda: bundle)
        monitor._stop_event.set()
        with monitor._lock:
            st_def = pngtuber_audio._make_avatar_state()
            st_def["device_connected"] = bool(default_cfg.get("obs_input_name"))
            st_def["device_name"] = default_cfg.get("obs_input_name", "")
            st_guest = pngtuber_audio._make_avatar_state()
            st_guest["device_connected"] = bool(guest_cfg.get("obs_input_name"))
            st_guest["device_name"] = guest_cfg.get("obs_input_name", "")
            monitor._avatar_states = {
                "": st_def,
                "guest": st_guest,
            }
        return monitor, bundle

    def test_crosstalk_margin_zero_disabled(self):
        monitor, bundle = self._make_monitor_and_bundle(
            {"obs_input_name": "Mic A", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 0.0},
            {"obs_input_name": "Mic B", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 0.0},
        )
        monitor._update_all_levels({"Mic A": 0.50, "Mic B": 0.06}, bundle)
        guest = monitor.get_level("guest")
        self.assertTrue(guest["speaking"])
        self.assertFalse(guest["crosstalk_suppressed"])
        self.assertAlmostEqual(guest["level"], 0.06, places=4)
        self.assertAlmostEqual(guest["raw_level"], 0.06, places=4)

    def test_crosstalk_suppressed_when_other_mic_much_louder(self):
        monitor, bundle = self._make_monitor_and_bundle(
            {"obs_input_name": "Mic A", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 6.0},
            {"obs_input_name": "Mic B", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 6.0},
        )
        monitor._update_all_levels({"Mic A": 0.50, "Mic B": 0.06}, bundle)
        default_lv = monitor.get_level("")
        guest_lv = monitor.get_level("guest")

        self.assertTrue(default_lv["speaking"])
        self.assertFalse(default_lv["crosstalk_suppressed"])
        self.assertAlmostEqual(default_lv["level"], 0.50, places=4)

        self.assertFalse(guest_lv["speaking"])
        self.assertTrue(guest_lv["crosstalk_suppressed"])
        self.assertEqual(guest_lv["level"], 0.0)
        self.assertAlmostEqual(guest_lv["raw_level"], 0.06, places=4)

    def test_simultaneous_speech_not_suppressed(self):
        monitor, bundle = self._make_monitor_and_bundle(
            {"obs_input_name": "Mic A", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 6.0},
            {"obs_input_name": "Mic B", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 6.0},
        )
        monitor._update_all_levels({"Mic A": 0.30, "Mic B": 0.25}, bundle)
        default_lv = monitor.get_level("")
        guest_lv = monitor.get_level("guest")

        self.assertTrue(default_lv["speaking"])
        self.assertFalse(default_lv["crosstalk_suppressed"])
        self.assertTrue(guest_lv["speaking"])
        self.assertFalse(guest_lv["crosstalk_suppressed"])
        self.assertAlmostEqual(guest_lv["level"], 0.25, places=4)

    def test_same_device_name_not_suppressed(self):
        monitor, bundle = self._make_monitor_and_bundle(
            {"obs_input_name": "Mic A", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 12.0},
            {"obs_input_name": "Mic A", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 12.0},
        )
        monitor._update_all_levels({"Mic A": 0.40}, bundle)
        default_lv = monitor.get_level("")
        guest_lv = monitor.get_level("guest")

        self.assertTrue(default_lv["speaking"])
        self.assertFalse(default_lv["crosstalk_suppressed"])
        self.assertTrue(guest_lv["speaking"])
        self.assertFalse(guest_lv["crosstalk_suppressed"])

    def test_other_mic_below_threshold_or_muted_not_suppressed(self):
        monitor, bundle = self._make_monitor_and_bundle(
            {"obs_input_name": "Mic A", "open_threshold": 0.50, "close_threshold": 0.25, "crosstalk_margin_db": 6.0},
            {"obs_input_name": "Mic B", "open_threshold": 0.05, "close_threshold": 0.025, "crosstalk_margin_db": 6.0},
        )
        # Mic A は 0.20 だが open_threshold=0.50 未満のため発話中ではない
        monitor._update_all_levels({"Mic A": 0.20, "Mic B": 0.06}, bundle)
        guest_lv = monitor.get_level("guest")
        self.assertTrue(guest_lv["speaking"])
        self.assertFalse(guest_lv["crosstalk_suppressed"])

        # Mic A がミュート等で levels_by_name に無い場合も抑制しない
        monitor._update_all_levels({"Mic B": 0.06}, bundle)
        guest_lv2 = monitor.get_level("guest")
        self.assertTrue(guest_lv2["speaking"])
        self.assertFalse(guest_lv2["crosstalk_suppressed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
