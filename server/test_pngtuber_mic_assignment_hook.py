#!/usr/bin/env python3
"""
PngTuber マイク割当 API (/api/pngtuber/mic-assignment) 拡張フックのテスト (Issue #9)
実行: python3 server/test_pngtuber_mic_assignment_hook.py
"""

import json
import os
import subprocess
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


def _http_request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestPngTuberMicAssignmentHookSubprocess(unittest.TestCase):
    def test_returns_404_when_extension_absent_and_pngtuber_unaffected(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

        script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path, method="GET", body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

res = {
    "enabled": handler._PNGTUBER_MIC_ASSIGNMENT_ENABLED,
    "get_mic": req("/api/pngtuber/mic-assignment"),
    "post_mic": req("/api/pngtuber/mic-assignment", method="POST", body=b'{"mode":"mode_a"}'),
    "get_pngtuber_config": req("/api/pngtuber/config"),
}

srv.shutdown()
srv.server_close()
print(json.dumps(res))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(data["enabled"])
        self.assertEqual(data["get_mic"][0], 404)
        self.assertEqual(data["post_mic"][0], 404)
        self.assertEqual(data["get_pngtuber_config"][0], 200)
        self.assertNotIn("pngtuber_mic_assignment", proc.stderr)

    def test_delegates_get_and_post_to_dummy_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)

            dummy_mod = os.path.join(ext_mods, "pngtuber_mic_assignment.py")
            with open(dummy_mod, "w", encoding="utf-8") as f:
                f.write(
                    "_state = {'mode': 'default', 'calls': 0, 'get_tuple': False}\n"
                    "\n"
                    "def get_pngtuber_mic_assignment():\n"
                    "    if _state['get_tuple']:\n"
                    "        return ({'mode': _state['mode'], 'from_tuple': True}, 202)\n"
                    "    return {'mode': _state['mode'], 'calls': _state['calls'], 'source': 'dummy_ext'}\n"
                    "\n"
                    "def update_pngtuber_mic_assignment(data):\n"
                    "    _state['calls'] += 1\n"
                    "    if data.get('trigger') == 'tuple_400':\n"
                    "        return ({'error': 'rejected by extension'}, 400)\n"
                    "    if data.get('trigger') == 'value_error':\n"
                    "        raise ValueError('bad assignment payload')\n"
                    "    if 'get_tuple' in data:\n"
                    "        _state['get_tuple'] = bool(data['get_tuple'])\n"
                    "    if 'mode' in data:\n"
                    "        _state['mode'] = str(data['mode'])\n"
                    "    return {'status': 'ok', 'mode': _state['mode'], 'calls': _state['calls']}\n"
                )

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path, method="GET", body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

res = {
    "enabled": handler._PNGTUBER_MIC_ASSIGNMENT_ENABLED,
    "get_initial": req("/api/pngtuber/mic-assignment"),
    "post_update": req("/api/pngtuber/mic-assignment", method="POST", body=b'{"mode":"mode_b"}'),
    "get_after": req("/api/pngtuber/mic-assignment"),
    "post_tuple_400": req("/api/pngtuber/mic-assignment", method="POST", body=b'{"trigger":"tuple_400"}'),
    "post_value_error": req("/api/pngtuber/mic-assignment", method="POST", body=b'{"trigger":"value_error"}'),
    "post_bad_json": req("/api/pngtuber/mic-assignment", method="POST", body=b'{not-json'),
    "post_non_dict": req("/api/pngtuber/mic-assignment", method="POST", body=b'["a"]'),
    "post_enable_get_tuple": req("/api/pngtuber/mic-assignment", method="POST", body=b'{"get_tuple":true}'),
    "get_tuple": req("/api/pngtuber/mic-assignment"),
}

srv.shutdown()
srv.server_close()
print(json.dumps(res))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["enabled"])
            self.assertEqual(data["get_initial"][0], 200)
            self.assertEqual(data["get_initial"][1], {"mode": "default", "calls": 0, "source": "dummy_ext"})
            self.assertEqual(data["post_update"][0], 200)
            self.assertEqual(data["post_update"][1], {"status": "ok", "mode": "mode_b", "calls": 1})
            self.assertEqual(data["get_after"][0], 200)
            self.assertEqual(data["get_after"][1]["mode"], "mode_b")
            self.assertEqual(data["get_after"][1]["calls"], 1)
            self.assertEqual(data["post_tuple_400"][0], 400)
            self.assertEqual(data["post_tuple_400"][1], {"error": "rejected by extension"})
            self.assertEqual(data["post_value_error"][0], 400)
            self.assertIn("bad assignment payload", data["post_value_error"][1]["error"])
            self.assertEqual(data["post_bad_json"][0], 400)
            self.assertEqual(data["post_non_dict"][0], 400)
            # bad_json / non_dict did not reach update_pngtuber_mic_assignment, so calls == 4 after enable_get_tuple
            self.assertEqual(data["post_enable_get_tuple"][0], 200)
            self.assertEqual(data["post_enable_get_tuple"][1]["calls"], 4)
            self.assertEqual(data["get_tuple"][0], 202)
            self.assertEqual(data["get_tuple"][1], {"mode": "mode_b", "from_tuple": True})

    def test_commentviewer_profile_disables_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "pngtuber_mic_assignment.py"), "w", encoding="utf-8") as f:
                f.write(
                    "def get_pngtuber_mic_assignment(): return {'mode': 'x'}\n"
                    "def update_pngtuber_mic_assignment(data): return {'status': 'ok'}\n"
                )

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env["OBS_WIDGETS_SERVER_PROFILE"] = "commentviewer"

            script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path, method="GET", body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code

res = {
    "enabled": handler._PNGTUBER_MIC_ASSIGNMENT_ENABLED,
    "get_status": req("/api/pngtuber/mic-assignment"),
    "post_status": req("/api/pngtuber/mic-assignment", method="POST", body=b'{}'),
}

srv.shutdown()
srv.server_close()
print(json.dumps(res))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(data["enabled"])
            self.assertEqual(data["get_status"], 404)
            self.assertEqual(data["post_status"], 404)
            self.assertNotIn("import に失敗しました", proc.stderr)

    def test_broken_extension_warns_and_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "pngtuber_mic_assignment.py"), "w", encoding="utf-8") as f:
                f.write("def get_pngtuber_mic_assignment(): return {}\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path, method="GET", body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code

res = {
    "enabled": handler._PNGTUBER_MIC_ASSIGNMENT_ENABLED,
    "get_status": req("/api/pngtuber/mic-assignment"),
    "post_status": req("/api/pngtuber/mic-assignment", method="POST", body=b'{}'),
}

srv.shutdown()
srv.server_close()
print(json.dumps(res))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(data["enabled"])
            self.assertEqual(data["get_status"], 404)
            self.assertEqual(data["post_status"], 404)
            self.assertIn("pngtuber_mic_assignment.py", proc.stderr)
            self.assertIn("import に失敗しました", proc.stderr)


class TestPngTuberMicAssignmentWriteGuard(unittest.TestCase):
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

    def test_post_requires_token_for_non_local_clients_when_enabled(self):
        fake_token = "dummy-test-widget-token"
        get_mock = mock.Mock(return_value={"mode": "mode_a"})
        update_mock = mock.Mock(side_effect=lambda payload: {"status": "ok", "received": payload})

        with mock.patch.object(server_handler, "_PNGTUBER_MIC_ASSIGNMENT_ENABLED", True), \
             mock.patch.object(server_handler, "_get_pngtuber_mic_assignment", get_mock), \
             mock.patch.object(server_handler, "_update_pngtuber_mic_assignment", update_mock), \
             mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False), \
             mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token), \
             mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token):
            # 読み取り専用 GET はトークンなしで許可
            status, body = _http_request(f"{self.base_url}/api/pngtuber/mic-assignment")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8")), {"mode": "mode_a"})
            get_mock.assert_called_once_with()

            # トークンなし POST -> 401（委譲されない）
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/mic-assignment",
                method="POST",
                body=json.dumps({"mode": "mode_b"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 401)
            update_mock.assert_not_called()

            # 不正なトークン付き POST -> 401（委譲されない）
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/mic-assignment",
                method="POST",
                body=json.dumps({"mode": "mode_b"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Widget-Token": "wrong-token"},
            )
            self.assertEqual(status, 401)
            update_mock.assert_not_called()

            # 有効なトークン付き POST -> 200（委譲される）
            status, body = _http_request(
                f"{self.base_url}/api/pngtuber/mic-assignment",
                method="POST",
                body=json.dumps({"mode": "mode_b"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Widget-Token": fake_token},
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body.decode("utf-8")),
                {"status": "ok", "received": {"mode": "mode_b"}},
            )
            update_mock.assert_called_once_with({"mode": "mode_b"})

    def test_post_write_guard_applies_even_when_extension_disabled(self):
        fake_token = "dummy-test-widget-token"

        with mock.patch.object(server_handler, "_PNGTUBER_MIC_ASSIGNMENT_ENABLED", False), \
             mock.patch.object(server_handler, "_update_pngtuber_mic_assignment", None), \
             mock.patch.object(server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False), \
             mock.patch.object(server_handler, "get_or_create_api_token", return_value=fake_token), \
             mock.patch.object(server_config, "get_or_create_api_token", return_value=fake_token):
            # 非ローカル + トークンなし -> 401
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/mic-assignment",
                method="POST",
                body=b'{"mode":"mode_b"}',
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 401)

            # 非ローカル + 有効トークン -> 404（拡張なし）
            status, _ = _http_request(
                f"{self.base_url}/api/pngtuber/mic-assignment",
                method="POST",
                body=b'{"mode":"mode_b"}',
                headers={"Content-Type": "application/json", "X-Widget-Token": fake_token},
            )
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
