#!/usr/bin/env python3
"""
任意拡張モジュール subtitle_mute_link の起動配線テスト (Issue #37)
実行: python3 server/test_subtitle_mute_link_wiring.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_server_startup_probe(env):
    """サブプロセスで server.py の起動経路を安全に実行し、結果を JSON で返す。"""
    script = r"""
import importlib.util
import json
import os
import sys
from unittest import mock

repo_root = os.getcwd()
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from server import handler

mod_loaded = sys.modules.get("server.subtitle_mute_link")
calls_before_run = getattr(mod_loaded, "CALL_COUNT", None) if mod_loaded is not None else None

spec = importlib.util.spec_from_file_location(
    "obs_widgets_server_entry", os.path.join(repo_root, "server.py")
)
server_entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_entry)

served = {"count": 0}


class _DummyServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 0)
        self.closed = False

    def serve_forever(self):
        served["count"] += 1
        raise KeyboardInterrupt()

    def shutdown(self):
        pass

    def server_close(self):
        self.closed = True


dummy_srv = _DummyServer()

with mock.patch.object(server_entry, "_start_http_loopback_servers", return_value=[dummy_srv]), \
     mock.patch.object(server_entry, "_start_tls_server_if_configured", return_value=None):
    server_entry.run()

calls_after_run = getattr(mod_loaded, "CALL_COUNT", None) if mod_loaded is not None else None

print(json.dumps({
    "enabled": handler._SUBTITLE_MUTE_LINK_ENABLED,
    "calls_before_run": calls_before_run,
    "calls_after_run": calls_after_run,
    "served_count": served["count"],
    "server_closed": dummy_srv.closed,
}))
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
    return data, proc.stderr


class TestSubtitleMuteLinkWiring(unittest.TestCase):
    def test_absent_module_is_silent_and_keeps_server_working(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

        data, stderr = _run_server_startup_probe(env)
        self.assertFalse(data["enabled"])
        self.assertIsNone(data["calls_before_run"])
        self.assertIsNone(data["calls_after_run"])
        self.assertEqual(data["served_count"], 1)
        self.assertTrue(data["server_closed"])
        self.assertNotIn("subtitle_mute_link", stderr)

    def test_present_module_starts_once_on_server_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "subtitle_mute_link.py"), "w", encoding="utf-8") as f:
                f.write(
                    "CALL_COUNT = 0\n"
                    "\n"
                    "def start_subtitle_mute_link():\n"
                    "    global CALL_COUNT\n"
                    "    CALL_COUNT += 1\n"
                )

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            data, stderr = _run_server_startup_probe(env)
            self.assertTrue(data["enabled"])
            # import server.handler の時点では起動せず、server.py run() で1回だけ起動する
            self.assertEqual(data["calls_before_run"], 0)
            self.assertEqual(data["calls_after_run"], 1)
            self.assertEqual(data["served_count"], 1)
            self.assertNotIn("警告: subtitle_mute_link", stderr)

    def test_startup_exception_warns_and_does_not_block_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "subtitle_mute_link.py"), "w", encoding="utf-8") as f:
                f.write(
                    "CALL_COUNT = 0\n"
                    "\n"
                    "def start_subtitle_mute_link():\n"
                    "    global CALL_COUNT\n"
                    "    CALL_COUNT += 1\n"
                    "    raise RuntimeError('simulated startup failure')\n"
                )

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            data, stderr = _run_server_startup_probe(env)
            self.assertTrue(data["enabled"])
            self.assertEqual(data["calls_before_run"], 0)
            self.assertEqual(data["calls_after_run"], 1)
            self.assertEqual(data["served_count"], 1)
            self.assertIn("subtitle_mute_link の起動に失敗しました", stderr)
            self.assertIn("simulated startup failure", stderr)

    def test_commentviewer_profile_disables_module_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "subtitle_mute_link.py"), "w", encoding="utf-8") as f:
                f.write(
                    "CALL_COUNT = 0\n"
                    "def start_subtitle_mute_link():\n"
                    "    global CALL_COUNT\n"
                    "    CALL_COUNT += 1\n"
                )

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env["OBS_WIDGETS_SERVER_PROFILE"] = "commentviewer"

            data, stderr = _run_server_startup_probe(env)
            self.assertFalse(data["enabled"])
            self.assertIsNone(data["calls_before_run"])
            self.assertIsNone(data["calls_after_run"])
            self.assertEqual(data["served_count"], 1)
            self.assertNotIn("import に失敗しました", stderr)
            self.assertNotIn("subtitle_mute_link", stderr)

    def test_broken_module_missing_symbol_warns_and_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "subtitle_mute_link.py"), "w", encoding="utf-8") as f:
                f.write("OTHER_SYMBOL = 1\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            data, stderr = _run_server_startup_probe(env)
            self.assertFalse(data["enabled"])
            self.assertEqual(data["served_count"], 1)
            self.assertIn("subtitle_mute_link.py", stderr)
            self.assertIn("import に失敗しました", stderr)

    def test_non_callable_symbol_warns_and_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            with open(os.path.join(ext_mods, "subtitle_mute_link.py"), "w", encoding="utf-8") as f:
                f.write("start_subtitle_mute_link = 'not-callable'\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            data, stderr = _run_server_startup_probe(env)
            self.assertFalse(data["enabled"])
            self.assertEqual(data["served_count"], 1)
            self.assertIn("subtitle_mute_link.py", stderr)
            self.assertIn("import に失敗しました", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
