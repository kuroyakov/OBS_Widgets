#!/usr/bin/env python3
"""
subtitle_mute_link の起動配線テスト (Issue #37 / Issue #39)
実行: python3 server/test_subtitle_mute_link_wiring.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_server_startup_probe(env, *, preamble=""):
    """サブプロセスで server.py の起動経路を安全に実行し、結果を JSON で返す。"""
    script = (
        r"""
import importlib
import importlib.util
import json
import os
import sys
from unittest import mock

repo_root = os.getcwd()
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

PROBE_STATE = {"start_calls": 0, "stop_calls": 0}
"""
        + preamble
        + r"""

from server import handler

calls_before_run = PROBE_STATE["start_calls"]

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

print(json.dumps({
    "enabled": handler._SUBTITLE_MUTE_LINK_ENABLED,
    "calls_before_run": calls_before_run,
    "calls_after_run": PROBE_STATE["start_calls"],
    "stop_calls": PROBE_STATE["stop_calls"],
    "served_count": served["count"],
    "server_closed": dummy_srv.closed,
}))
"""
    )
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


_HIDE_BUILTIN_PREAMBLE = r"""
from server import extensions as _ext
_orig_base_exists = _ext._base_module_exists
_orig_import_module = importlib.import_module
_orig_isfile = os.path.isfile

def _patched_base_exists(base_dir, module_name):
    if module_name == "subtitle_mute_link":
        return False
    return _orig_base_exists(base_dir, module_name)

def _patched_import_module(name, package=None):
    if name == "server.subtitle_mute_link":
        raise ModuleNotFoundError("No module named 'server.subtitle_mute_link'", name="server.subtitle_mute_link")
    return _orig_import_module(name, package=package)

def _patched_isfile(path):
    if os.path.normpath(str(path)).endswith(os.path.join("server", "subtitle_mute_link.py")):
        return False
    return _orig_isfile(path)

_ext._base_module_exists = _patched_base_exists
importlib.import_module = _patched_import_module
os.path.isfile = _patched_isfile
"""


class TestSubtitleMuteLinkWiring(unittest.TestCase):
    def test_builtin_module_starts_once_on_server_run_and_stops_on_exit(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

        preamble = r"""
from server import subtitle_mute_link as _sml
_orig_start = _sml.start_subtitle_mute_link
_orig_stop = _sml.stop_subtitle_mute_link

def _spy_start():
    PROBE_STATE["start_calls"] += 1
    return False

def _spy_stop():
    PROBE_STATE["stop_calls"] += 1
    _orig_stop()

_sml.start_subtitle_mute_link = _spy_start
_sml.stop_subtitle_mute_link = _spy_stop
"""
        data, stderr = _run_server_startup_probe(env, preamble=preamble)
        self.assertTrue(data["enabled"])
        # import server.handler の時点では起動せず、server.py run() で1回だけ起動する
        self.assertEqual(data["calls_before_run"], 0)
        self.assertEqual(data["calls_after_run"], 1)
        self.assertEqual(data["stop_calls"], 1)
        self.assertEqual(data["served_count"], 1)
        self.assertTrue(data["server_closed"])
        self.assertNotIn("警告: subtitle_mute_link", stderr)

    def test_startup_exception_warns_and_does_not_block_server(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

        preamble = r"""
from server import subtitle_mute_link as _sml

def _failing_start():
    PROBE_STATE["start_calls"] += 1
    raise RuntimeError("simulated startup failure")

_sml.start_subtitle_mute_link = _failing_start
"""
        data, stderr = _run_server_startup_probe(env, preamble=preamble)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["calls_before_run"], 0)
        self.assertEqual(data["calls_after_run"], 1)
        self.assertEqual(data["served_count"], 1)
        self.assertIn("subtitle_mute_link の起動に失敗しました", stderr)
        self.assertIn("simulated startup failure", stderr)

    def test_commentviewer_profile_disables_module_silently(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env["OBS_WIDGETS_SERVER_PROFILE"] = "commentviewer"

        data, stderr = _run_server_startup_probe(env)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["calls_before_run"], 0)
        self.assertEqual(data["calls_after_run"], 0)
        self.assertEqual(data["served_count"], 1)
        self.assertNotIn("import に失敗しました", stderr)
        self.assertNotIn("subtitle_mute_link", stderr)

    def test_absent_module_is_silent_and_keeps_server_working(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

        data, stderr = _run_server_startup_probe(env, preamble=_HIDE_BUILTIN_PREAMBLE)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["calls_before_run"], 0)
        self.assertEqual(data["calls_after_run"], 0)
        self.assertEqual(data["served_count"], 1)
        self.assertTrue(data["server_closed"])
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

            data, stderr = _run_server_startup_probe(env, preamble=_HIDE_BUILTIN_PREAMBLE)
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

            data, stderr = _run_server_startup_probe(env, preamble=_HIDE_BUILTIN_PREAMBLE)
            self.assertFalse(data["enabled"])
            self.assertEqual(data["served_count"], 1)
            self.assertIn("subtitle_mute_link.py", stderr)
            self.assertIn("import に失敗しました", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
