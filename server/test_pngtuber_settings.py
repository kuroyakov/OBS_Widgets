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

            for bad in ("", "  ", "default", "../evil", "a b", "a/b", "x" * 65, 123):
                with self.assertRaises(ValueError, msg=f"avatar={bad!r}"):
                    pngtuber_audio.save_pngtuber_config(
                        {"obs_input_name": "Nope"}, config_path=cfg_path, avatar=bad
                    )
            with open(cfg_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertNotIn("../evil", on_disk["avatars"])
            self.assertNotIn("default", on_disk["avatars"])

    def test_avatar_image_upload_updates_avatar_slot_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "pngtuber_config.json")
            self._write_initial(cfg_path)
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
            self.assertEqual(
                on_disk["avatars"]["alpha"]["images"]["mouth_open_eye_open"],
                "images/alpha_face.png",
            )
            self.assertNotIn("images", on_disk)
            self.assertEqual(on_disk["avatars"]["beta"]["obs_input_name"], "Beta Mic")

            with self.assertRaises(ValueError):
                pngtuber_audio.save_pngtuber_image_upload(
                    "mouth_open_eye_open",
                    "x.png",
                    MINIMAL_PNG,
                    pngtuber_dir=widget_dir,
                    config_path=cfg_path,
                    avatar="../evil",
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

        # 不正なアバター名は 400
        payload = json.dumps({"avatar": "../evil", "obs_input_name": "X"}).encode("utf-8")
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/config",
            method="POST",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

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

        # 非 .png や壊れたバイト列は 400
        ct_jpg, jpg_body = _build_multipart_body("file", "photo.jpg", MINIMAL_PNG, content_type="image/jpeg")
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open",
            method="POST",
            body=jpg_body,
            headers={"Content-Type": ct_jpg},
        )
        self.assertEqual(status, 400)

        ct_fake, fake_body = _build_multipart_body("file", "fake.png", b"hello world")
        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/mouth_open_eye_open",
            method="POST",
            body=fake_body,
            headers={"Content-Type": ct_fake},
        )
        self.assertEqual(status, 400)

        status, _ = _http_request(
            f"{self.base_url}/api/pngtuber/image/not_a_slot",
            method="POST",
            body=mp_body,
            headers={"Content-Type": ct},
        )
        self.assertEqual(status, 400)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
