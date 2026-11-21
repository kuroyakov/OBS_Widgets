#!/usr/bin/env python3
"""
拡張ディレクトリ機構のテスト (Issue #220)
実行: python3 server/test_extensions.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import extensions  # noqa: E402


def _http_request(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestExtensionsUnit(unittest.TestCase):
    def test_resolve_extensions_dir_empty_or_unset(self):
        warn = io.StringIO()
        self.assertIsNone(extensions.resolve_extensions_dir("", warn_stream=warn))
        self.assertIsNone(extensions.resolve_extensions_dir("   ", warn_stream=warn))
        self.assertEqual(warn.getvalue(), "")

    def test_resolve_extensions_dir_relative_warns_and_disables(self):
        warn = io.StringIO()
        self.assertIsNone(extensions.resolve_extensions_dir("relative/path", warn_stream=warn))
        self.assertIn("絶対パス", warn.getvalue())

    def test_resolve_extensions_dir_missing_warns_and_disables(self):
        warn = io.StringIO()
        self.assertIsNone(
            extensions.resolve_extensions_dir("/nonexistent/obs_widgets_ext_dir_220", warn_stream=warn)
        )
        self.assertIn("存在しない", warn.getvalue())

    def test_resolve_extensions_dir_default_fallback_and_explicit_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_with_ext = os.path.join(tmp, "with_ext")
            base_no_ext = os.path.join(tmp, "no_ext")
            other_ext = os.path.join(tmp, "other_ext")
            default_ext = os.path.join(base_with_ext, "extensions")
            os.makedirs(default_ext)
            os.makedirs(base_no_ext)
            os.makedirs(other_ext)

            prev = os.environ.pop(extensions.ENV_VAR_NAME, None)
            try:
                warn = io.StringIO()
                self.assertEqual(
                    extensions.resolve_extensions_dir(base_dir=base_with_ext, warn_stream=warn),
                    os.path.realpath(default_ext),
                )
                self.assertIsNone(
                    extensions.resolve_extensions_dir(base_dir=base_no_ext, warn_stream=warn)
                )
                self.assertEqual(warn.getvalue(), "")

                # 空文字が明示的に設定されている場合は無効化（extensions/ へフォールバックしない）
                os.environ[extensions.ENV_VAR_NAME] = ""
                self.assertIsNone(
                    extensions.resolve_extensions_dir(base_dir=base_with_ext, warn_stream=warn)
                )
                self.assertEqual(warn.getvalue(), "")

                # 環境変数が設定されている場合はそちらが優先
                os.environ[extensions.ENV_VAR_NAME] = other_ext
                self.assertEqual(
                    extensions.resolve_extensions_dir(base_dir=base_with_ext, warn_stream=warn),
                    os.path.realpath(other_ext),
                )
                self.assertEqual(warn.getvalue(), "")
            finally:
                if prev is None:
                    os.environ.pop(extensions.ENV_VAR_NAME, None)
                else:
                    os.environ[extensions.ENV_VAR_NAME] = prev

    def test_discover_static_mounts_skips_reserved_and_warns_on_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = os.path.join(tmp, "base")
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(os.path.join(base_dir, "Clock"))
            with open(os.path.join(base_dir, "Clock", "Date.html"), "w", encoding="utf-8") as f:
                f.write("<html>base-clock</html>")
            os.makedirs(os.path.join(ext_dir, "Clock"))
            os.makedirs(os.path.join(ext_dir, "DummyWidget"))
            os.makedirs(os.path.join(ext_dir, "server_modules"))
            os.makedirs(os.path.join(ext_dir, ".hidden_dir"))
            with open(os.path.join(ext_dir, "plain_file.txt"), "w", encoding="utf-8") as f:
                f.write("ignored")

            warn = io.StringIO()
            mounts = extensions.discover_static_mounts(base_dir, ext_dir, warn_stream=warn)

            self.assertEqual(list(mounts.keys()), ["DummyWidget"])
            self.assertEqual(mounts["DummyWidget"], os.path.realpath(os.path.join(ext_dir, "DummyWidget")))
            self.assertIn("Clock", warn.getvalue())
            self.assertIn("BASE_DIR", warn.getvalue())

    def test_discover_static_mounts_allows_empty_base_dir_and_blocks_real_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = os.path.join(tmp, "base")
            ext_dir = os.path.join(tmp, "ext")
            # 空ディレクトリ (直下が空) → 衝突とせずマウント
            os.makedirs(os.path.join(base_dir, "Controller"))
            os.makedirs(os.path.join(ext_dir, "Controller"))
            with open(os.path.join(ext_dir, "Controller", "dock.html"), "w", encoding="utf-8") as f:
                f.write("<html>ext-dock</html>")
            # 空ディレクトリ (サブディレクトリも空) → 衝突とせずマウント
            os.makedirs(os.path.join(base_dir, "NestedEmpty", "sub"))
            os.makedirs(os.path.join(ext_dir, "NestedEmpty"))
            with open(os.path.join(ext_dir, "NestedEmpty", "index.html"), "w", encoding="utf-8") as f:
                f.write("<html>ext-nested</html>")
            # サブディレクトリに実ファイルあり → 衝突 (BASE_DIR 優先)
            os.makedirs(os.path.join(base_dir, "NestedReal", "sub"))
            with open(os.path.join(base_dir, "NestedReal", "sub", "data.txt"), "w", encoding="utf-8") as f:
                f.write("real")
            os.makedirs(os.path.join(ext_dir, "NestedReal"))
            # 同名ファイル → 衝突 (BASE_DIR 優先)
            with open(os.path.join(base_dir, "FileEntry"), "w", encoding="utf-8") as f:
                f.write("file")
            os.makedirs(os.path.join(ext_dir, "FileEntry"))

            log = io.StringIO()
            mounts = extensions.discover_static_mounts(base_dir, ext_dir, warn_stream=log)
            output = log.getvalue()

            self.assertEqual(sorted(mounts.keys()), ["Controller", "NestedEmpty"])
            self.assertEqual(mounts["Controller"], os.path.realpath(os.path.join(ext_dir, "Controller")))
            self.assertEqual(mounts["NestedEmpty"], os.path.realpath(os.path.join(ext_dir, "NestedEmpty")))
            self.assertIn("情報", output)
            self.assertIn("Controller", output)
            self.assertIn("空ディレクトリ", output)
            self.assertIn("NestedReal", output)
            self.assertIn("FileEntry", output)

    def test_load_optional_module_fallback_and_cross_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = os.path.join(tmp, "base")
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(os.path.join(base_dir, "server"))
            os.makedirs(os.path.join(ext_dir, "server_modules"))

            with open(os.path.join(ext_dir, "server_modules", "dummy_ext_a.py"), "w", encoding="utf-8") as f:
                f.write("from server.config import BASE_DIR\nVALUE = 'from_ext_a'\n")
            with open(os.path.join(ext_dir, "server_modules", "dummy_ext_b.py"), "w", encoding="utf-8") as f:
                f.write("from server.dummy_ext_a import VALUE\nCOMBINED = VALUE + '+b'\n")

            try:
                mod_a = extensions.load_optional_module(
                    "dummy_ext_a", base_dir=base_dir, extensions_dir=ext_dir
                )
                mod_b = extensions.load_optional_module(
                    "dummy_ext_b", base_dir=base_dir, extensions_dir=ext_dir
                )
                self.assertEqual(mod_a.VALUE, "from_ext_a")
                self.assertEqual(mod_b.COMBINED, "from_ext_a+b")
                self.assertIs(sys.modules["server.dummy_ext_a"], mod_a)
                self.assertIs(sys.modules["server.dummy_ext_b"], mod_b)
            finally:
                for name in ("dummy_ext_a", "dummy_ext_b"):
                    sys.modules.pop(f"server.{name}", None)
                    server_pkg = sys.modules.get("server")
                    if server_pkg is not None and hasattr(server_pkg, name):
                        delattr(server_pkg, name)

    def test_load_optional_module_no_fallback_when_base_file_exists_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = os.path.join(tmp, "base")
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(os.path.join(base_dir, "server"))
            os.makedirs(os.path.join(ext_dir, "server_modules"))

            # BASE_DIR 側にファイルが存在するが、通常 import が失敗するケース
            with open(os.path.join(base_dir, "server", "dummy_broken_base.py"), "w", encoding="utf-8") as f:
                f.write("raise ImportError('broken base')\n")
            with open(os.path.join(ext_dir, "server_modules", "dummy_broken_base.py"), "w", encoding="utf-8") as f:
                f.write("VALUE = 'should_not_load'\n")

            warn = io.StringIO()
            with self.assertRaises(ImportError):
                extensions.load_optional_module(
                    "dummy_broken_base",
                    base_dir=base_dir,
                    extensions_dir=ext_dir,
                    warn_stream=warn,
                )
            self.assertNotIn("server.dummy_broken_base", sys.modules)
            self.assertIn("名前衝突", warn.getvalue())

    def test_load_optional_module_collision_base_wins_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(os.path.join(ext_dir, "server_modules"))
            with open(os.path.join(ext_dir, "server_modules", "event_log.py"), "w", encoding="utf-8") as f:
                f.write("FROM_EXT = True\n")

            warn = io.StringIO()
            mod = extensions.load_optional_module(
                "event_log",
                base_dir=REPO_ROOT,
                extensions_dir=ext_dir,
                warn_stream=warn,
            )
            self.assertFalse(getattr(mod, "FROM_EXT", False))
            self.assertIn("event_log", warn.getvalue())
            self.assertIn("BASE_DIR", warn.getvalue())

    def test_translate_extension_path_blocks_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_root = os.path.join(tmp, "ext", "DummyWidget")
            os.makedirs(ext_root)
            outside = os.path.join(tmp, "outside.txt")
            with open(outside, "w", encoding="utf-8") as f:
                f.write("secret")
            os.symlink(outside, os.path.join(ext_root, "escape.txt"))

            mounts = {"DummyWidget": os.path.realpath(ext_root)}
            self.assertIsNone(extensions.translate_extension_path("/DummyWidget/escape.txt", mounts))
            self.assertIsNone(extensions.translate_extension_path("/DummyWidget/../outside.txt", mounts))


class TestExtensionsIntegration(unittest.TestCase):
    # 以下の3テストは、リポジトリ直下の extensions/ に実モジュール群がある
    # 配置 (開発者の個人用レイアウト) を前提とするため、その配置が無い
    # 公開リポジトリ単体ではスキップする。拡張機構そのものの検証は
    # 一時フィクスチャを使うテスト群が担う。
    def test_unset_env_uses_default_extensions_dir_and_enables_all_modules(self):
        if not os.path.isdir(os.path.join(REPO_ROOT, "extensions", "server_modules")):
            self.skipTest("repo-local extensions/ layout not present")
        env = os.environ.copy()
        env.pop("OBS_WIDGETS_EXTENSIONS_DIR", None)
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)
        code = (
            "import json; "
            "from server import handler; "
            "from server.launcher import execute_launcher_action; "
            "print(json.dumps({"
            "'ext_dir': handler._EXTENSIONS_DIR, "
            "'mounts': sorted(handler._EXTENSION_STATIC_MOUNTS.keys()), "
            "'controller': handler._CONTROLLER_ENABLED, "
            "'avatar': handler._AVATAR_LAYOUTS_ENABLED, "
            "'cubase': handler._CUBASE_BRIDGE_ENABLED, "
            "'pngtuber': handler._PNGTUBER_ENABLED, "
            "'launcher_callable': callable(execute_launcher_action)"
            "}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data["ext_dir"], os.path.realpath(os.path.join(REPO_ROOT, "extensions")))
        self.assertEqual(data["mounts"], ["Controller", "CubaseBridge"])
        self.assertTrue(data["controller"])
        self.assertTrue(data["avatar"])
        self.assertTrue(data["cubase"])
        self.assertTrue(data["pngtuber"])
        self.assertTrue(data["launcher_callable"])
        self.assertNotIn("[extensions]", proc.stderr)

    def test_empty_env_explicitly_disables_default_extensions_dir(self):
        env = os.environ.copy()
        env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)
        code = (
            "import json; "
            "from server import handler; "
            "print(json.dumps({"
            "'ext_dir': handler._EXTENSIONS_DIR, "
            "'mounts': handler._EXTENSION_STATIC_MOUNTS, "
            "'controller': handler._CONTROLLER_ENABLED, "
            "'avatar': handler._AVATAR_LAYOUTS_ENABLED, "
            "'cubase': handler._CUBASE_BRIDGE_ENABLED, "
            "'pngtuber': handler._PNGTUBER_ENABLED"
            "}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIsNone(data["ext_dir"])
        self.assertEqual(data["mounts"], {})
        self.assertFalse(data["controller"])
        self.assertFalse(data["avatar"])
        self.assertFalse(data["cubase"])
        self.assertTrue(data["pngtuber"])
        self.assertNotIn("[extensions]", proc.stderr)

    def test_default_extensions_http_smoke(self):
        if not os.path.isdir(os.path.join(REPO_ROOT, "extensions", "server_modules")):
            self.skipTest("repo-local extensions/ layout not present")
        """環境変数未設定の状態で実サーバーを起動し、Controller / CubaseBridge が 200 で配信されることを検証。"""
        env = os.environ.copy()
        env.pop("OBS_WIDGETS_EXTENSIONS_DIR", None)
        env.pop("OBS_WIDGETS_SERVER_PROFILE", None)
        script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server.handler import UnifiedWidgetHandler

srv = ThreadingHTTPServer(("127.0.0.1", 0), UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, len(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, len(e.read())

results = {
    "controller_index": req("/Controller/"),
    "controller_dock": req("/Controller/dock.html"),
    "cubase_receiver": req("/CubaseBridge/receiver.html"),
    "cubase_overlay": req("/CubaseBridge/overlay.html"),
    "shared_env_config": req("/shared/env-config.js"),
    "api_cubase_state": req("/api/cubase-bridge/state"),
    "api_avatar_layouts": req("/api/avatar-layouts"),
}

srv.shutdown()
srv.server_close()
print(json.dumps(results))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        for key, (status, size) in res.items():
            self.assertEqual(status, 200, f"{key} returned {status}")
            self.assertGreater(size, 0, f"{key} returned empty body")

    def test_load_optional_modules_for_launcher_subprocess(self):
        if not os.path.isdir(os.path.join(REPO_ROOT, "extensions", "server_modules")):
            self.skipTest("repo-local extensions/ layout not present")
        """obs_mothership.lua / start_server.command / stop_server.command 相当の短命 subprocess から launcher を読み込めること。"""
        env = os.environ.copy()
        env.pop("OBS_WIDGETS_EXTENSIONS_DIR", None)
        code = (
            "import json; "
            "from server.extensions import load_optional_modules; "
            "m = load_optional_modules(('browser_refresh', 'display_recapture', 'launcher')); "
            "print(json.dumps({"
            "'execute': callable(m.execute_launcher_action), "
            "'load_cfg': callable(m.load_launcher_config), "
            "'open_chrome': callable(m.open_chrome_url), "
            "'jimaku': bool(m.JIMAKUCHAN_URL), "
            "'keylab': bool(m.KEYBOARD_URL)"
            "}))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(all(data.values()), data)

    def test_static_mount_collision_and_security_guards_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            dummy_dir = os.path.join(ext_dir, "DummyWidget")
            os.makedirs(os.path.join(dummy_dir, "sub"))
            os.makedirs(os.path.join(dummy_dir, "empty_dir"))
            os.makedirs(os.path.join(ext_dir, "Clock"))
            os.makedirs(os.path.join(ext_dir, "server_modules"))

            with open(os.path.join(dummy_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("<html>dummy-ext-index</html>")
            with open(os.path.join(dummy_dir, "sub", "app.js"), "w", encoding="utf-8") as f:
                f.write("console.log('dummy');")
            with open(os.path.join(dummy_dir, "twitch_config.json"), "w", encoding="utf-8") as f:
                f.write('{"secret": true}')
            with open(os.path.join(dummy_dir, "cubase_bridge_config.json"), "w", encoding="utf-8") as f:
                f.write('{"secret": true}')
            with open(os.path.join(dummy_dir, "secret.py"), "w", encoding="utf-8") as f:
                f.write("SECRET = 1\n")
            with open(os.path.join(dummy_dir, ".hidden"), "w", encoding="utf-8") as f:
                f.write("hidden")
            with open(os.path.join(dummy_dir, "env-config.local.js"), "w", encoding="utf-8") as f:
                f.write("window.LOCAL = true;")
            with open(os.path.join(ext_dir, "Clock", "Date.html"), "w", encoding="utf-8") as f:
                f.write("<html>EXT_CLOCK_SHOULD_NOT_WIN</html>")
            with open(os.path.join(ext_dir, "server_modules", "helper.py"), "w", encoding="utf-8") as f:
                f.write("X = 1\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server.handler import UnifiedWidgetHandler

srv = ThreadingHTTPServer(("127.0.0.1", 0), UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

def req(path, method="GET"):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

results = {
    "dummy_index": req("/DummyWidget/index.html"),
    "dummy_slash": req("/DummyWidget/"),
    "dummy_head": req("/DummyWidget/index.html", "HEAD"),
    "dummy_js": req("/DummyWidget/sub/app.js"),
    "deny_twitch_cfg": req("/DummyWidget/twitch_config.json"),
    "deny_twitch_cfg_head": req("/DummyWidget/twitch_config.json", "HEAD"),
    "deny_cubase_cfg": req("/DummyWidget/cubase_bridge_config.json"),
    "deny_py": req("/DummyWidget/secret.py"),
    "deny_hidden": req("/DummyWidget/.hidden"),
    "deny_dirlist": req("/DummyWidget/empty_dir/"),
    "deny_server_modules": req("/server_modules/helper.py"),
    "local_env_js": req("/DummyWidget/env-config.local.js"),
    "clock_collision": req("/Clock/Date.html"),
}

# 非ローカルクライアントでは env-config.local.js が遮断されることも検証
class _FakeRemote(UnifiedWidgetHandler):
    def __init__(self):
        self.client_address = ("203.0.113.10", 12345)

results["remote_env_js_blocked"] = _FakeRemote()._is_static_blocked("/DummyWidget/env-config.local.js")

srv.shutdown()
srv.server_close()
print(json.dumps(results))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

            # 名前衝突の警告が起動時に標準エラーへ出ていること
            self.assertIn("Clock", proc.stderr)
            self.assertIn("BASE_DIR", proc.stderr)

            res = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(res["dummy_index"][0], 200)
            self.assertIn("dummy-ext-index", res["dummy_index"][1])
            self.assertEqual(res["dummy_slash"][0], 200)
            self.assertIn("dummy-ext-index", res["dummy_slash"][1])
            self.assertEqual(res["dummy_head"][0], 200)
            self.assertEqual(res["dummy_js"][0], 200)
            self.assertIn("console.log('dummy')", res["dummy_js"][1])

            # 既存の保護 (deny リスト・隠しファイル・ディレクトリ一覧・server_modules・env-config.local.js)
            self.assertEqual(res["deny_twitch_cfg"][0], 403)
            self.assertEqual(res["deny_twitch_cfg_head"][0], 403)
            self.assertEqual(res["deny_cubase_cfg"][0], 403)
            self.assertEqual(res["deny_py"][0], 403)
            self.assertEqual(res["deny_hidden"][0], 403)
            self.assertEqual(res["deny_dirlist"][0], 403)
            self.assertEqual(res["deny_server_modules"][0], 403)
            self.assertEqual(res["local_env_js"][0], 200)
            self.assertTrue(res["remote_env_js_blocked"])

            # 名前衝突時は BASE_DIR 側の Clock/index.html が勝つこと
            self.assertEqual(res["clock_collision"][0], 200)
            self.assertNotIn("EXT_CLOCK_SHOULD_NOT_WIN", res["clock_collision"][1])

    def test_handler_loads_optional_modules_from_extension_when_absent_in_base(self):
        """BASE_DIR 側に個人モジュールが不在の仮想ツリーで、拡張側 server_modules/ から読み込まれることを検証。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_base = os.path.join(tmp, "repo")
            fake_server = os.path.join(fake_base, "server")
            os.makedirs(fake_server)

            # 公開側モジュールのみを仮想ツリーへコピー (個人側6モジュールと pngtuber_audio は置かない)
            public_modules = [
                "__init__.py",
                "config.py",
                "event_log.py",
                "extensions.py",
                "feed_log.py",
                "handler.py",
                "obs_ws.py",
                "translator.py",
                "twitch_api.py",
                "voicevox.py",
                "youtube_chat.py",
            ]
            for name in public_modules:
                shutil.copy2(os.path.join(REPO_ROOT, "server", name), os.path.join(fake_server, name))

            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            os.makedirs(os.path.join(ext_dir, "ExtWidget"))
            with open(os.path.join(ext_dir, "ExtWidget", "index.html"), "w", encoding="utf-8") as f:
                f.write("<html>ext-widget-ok</html>")

            # ダミーの avatar_layouts / cubase_bridge / browser_refresh / display_recapture / launcher / controller_status
            with open(os.path.join(ext_mods, "browser_refresh.py"), "w", encoding="utf-8") as f:
                f.write("def note_page_ready(name): return True\n")
            with open(os.path.join(ext_mods, "display_recapture.py"), "w", encoding="utf-8") as f:
                f.write("def recapture_displays(): return {'status': 'ok'}\n")
            with open(os.path.join(ext_mods, "launcher.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from server.browser_refresh import note_page_ready\n"
                    "from server.display_recapture import recapture_displays\n"
                    "def execute_launcher_action(req): return {'status': 'ok', 'source': 'ext'}\n"
                )
            with open(os.path.join(ext_mods, "controller_status.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from server.launcher import execute_launcher_action\n"
                    "def record_heartbeat(client): pass\n"
                    "def get_lan_network_info(): return {'lan_ips': [], 'local_hostname': 'ext-host'}\n"
                    "def get_all_launcher_status(config=None): return {'source': 'ext'}\n"
                )
            with open(os.path.join(ext_mods, "avatar_layouts.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from server.config import BASE_DIR\n"
                    "def load_avatar_layouts(): return {'both': [], 'partner': [], 'from_ext': True}\n"
                    "def save_avatar_layouts(data): return data\n"
                )
            with open(os.path.join(ext_mods, "cubase_bridge.py"), "w", encoding="utf-8") as f:
                f.write(
                    "def ingest_event(e): return True\n"
                    "def get_state(): return {'source': 'ext_cubase'}\n"
                    "def register_stream(): return None\n"
                    "def unregister_stream(q): pass\n"
                    "def wait_stream_update(q, timeout=1.0): return None\n"
                    "def is_stopped(): return False\n"
                )
            # pngtuber_audio は対象外のため、server_modules に置いても読み込まれないこと
            with open(os.path.join(ext_mods, "pngtuber_audio.py"), "w", encoding="utf-8") as f:
                f.write("SHOULD_NOT_BE_LOADED = True\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            script = r"""
import json, threading, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/avatar-layouts", timeout=5) as r:
    avatar_resp = json.loads(r.read().decode("utf-8"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/ExtWidget/index.html", timeout=5) as r:
    ext_html = r.read().decode("utf-8")

srv.shutdown()
srv.server_close()

print(json.dumps({
    "controller": handler._CONTROLLER_ENABLED,
    "avatar": handler._AVATAR_LAYOUTS_ENABLED,
    "cubase": handler._CUBASE_BRIDGE_ENABLED,
    "pngtuber": handler._PNGTUBER_ENABLED,
    "avatar_resp": avatar_resp,
    "ext_html": ext_html,
}))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=fake_base,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["controller"])
            self.assertTrue(data["avatar"])
            self.assertTrue(data["cubase"])
            self.assertFalse(data["pngtuber"])
            self.assertTrue(data["avatar_resp"].get("from_ext"))
            self.assertIn("ext-widget-ok", data["ext_html"])
            # pngtuber_audio は対象外なので警告も出ないこと
            self.assertNotIn("pngtuber_audio", proc.stderr)

    def test_handler_warns_when_extension_module_import_fails(self):
        """拡張側 server_modules/<name>.py が存在して import に失敗した場合、警告が出ることを検証。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_base = os.path.join(tmp, "repo")
            fake_server = os.path.join(fake_base, "server")
            os.makedirs(fake_server)
            for name in (
                "__init__.py",
                "config.py",
                "event_log.py",
                "extensions.py",
                "feed_log.py",
                "handler.py",
                "obs_ws.py",
                "translator.py",
                "twitch_api.py",
                "voicevox.py",
                "youtube_chat.py",
            ):
                shutil.copy2(os.path.join(REPO_ROOT, "server", name), os.path.join(fake_server, name))

            ext_dir = os.path.join(tmp, "ext")
            ext_mods = os.path.join(ext_dir, "server_modules")
            os.makedirs(ext_mods)
            # 意図的に ImportError を起こすダミー拡張モジュール
            broken_path = os.path.join(ext_mods, "avatar_layouts.py")
            with open(broken_path, "w", encoding="utf-8") as f:
                f.write("raise ImportError('intentional test failure')\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            proc = subprocess.run(
                [sys.executable, "-c", "from server import handler; assert not handler._AVATAR_LAYOUTS_ENABLED"],
                cwd=fake_base,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("avatar_layouts.py", proc.stderr)
            self.assertIn("import に失敗しました", proc.stderr)

    def test_commentviewer_profile_disables_extension_modules(self):
        """OBS_WIDGETS_SERVER_PROFILE=commentviewer では拡張側モジュールも無効のままになること。"""
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(os.path.join(ext_dir, "server_modules"))
            with open(os.path.join(ext_dir, "server_modules", "avatar_layouts.py"), "w", encoding="utf-8") as f:
                f.write("def load_avatar_layouts(): return {}\ndef save_avatar_layouts(d): return d\n")

            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ext_dir
            env["OBS_WIDGETS_SERVER_PROFILE"] = "commentviewer"

            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from server import handler; "
                    "assert not handler._CONTROLLER_ENABLED; "
                    "assert not handler._AVATAR_LAYOUTS_ENABLED; "
                    "assert not handler._CUBASE_BRIDGE_ENABLED; "
                    "assert not handler._PNGTUBER_ENABLED",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertNotIn("import に失敗しました", proc.stderr)

    def test_empty_base_dir_does_not_block_extension_http_serving(self):
        """BASE_DIR 側に空ディレクトリが残存していても拡張側ウィジェットが HTTP 200 で配信されること。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_base = os.path.join(tmp, "repo")
            fake_server = os.path.join(fake_base, "server")
            os.makedirs(fake_server)
            for name in (
                "__init__.py",
                "config.py",
                "event_log.py",
                "extensions.py",
                "feed_log.py",
                "handler.py",
                "obs_ws.py",
                "translator.py",
                "twitch_api.py",
                "voicevox.py",
                "youtube_chat.py",
            ):
                shutil.copy2(os.path.join(REPO_ROOT, "server", name), os.path.join(fake_server, name))

            # BASE_DIR 側に空の Controller/ が残存、extensions/Controller/ に実体がある状態
            os.makedirs(os.path.join(fake_base, "Controller"))
            ext_ctrl = os.path.join(fake_base, "extensions", "Controller")
            os.makedirs(ext_ctrl)
            with open(os.path.join(ext_ctrl, "dock.html"), "w", encoding="utf-8") as f:
                f.write("<html>ext-controller-dock</html>")

            env = os.environ.copy()
            env.pop("OBS_WIDGETS_EXTENSIONS_DIR", None)
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)

            script = r"""
import json, threading, urllib.request
from http.server import ThreadingHTTPServer
from server.handler import UnifiedWidgetHandler

srv = ThreadingHTTPServer(("127.0.0.1", 0), UnifiedWidgetHandler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()

with urllib.request.urlopen(f"http://127.0.0.1:{port}/Controller/dock.html", timeout=5) as r:
    status = r.status
    body = r.read().decode("utf-8")

srv.shutdown()
srv.server_close()
print(json.dumps({"status": status, "body": body}))
"""
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=fake_base,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(data["status"], 200)
            self.assertIn("ext-controller-dock", data["body"])
            self.assertIn("空ディレクトリ", proc.stderr)
            self.assertNotIn("無効化します", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
