import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from server import subtitle_lang

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSubtitleLangState(unittest.TestCase):
    def setUp(self):
        subtitle_lang.SUBTITLE_LANG_STATE["active_lang"] = "en"
        subtitle_lang.SUBTITLE_LANG_STATE["last_foreign_comment_ts"] = 0.0

    def test_normalize_subtitle_lang(self):
        self.assertEqual(subtitle_lang.normalize_subtitle_lang(""), "en")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("ja"), "en")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("auto"), "en")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("zh-TW"), "zh-TW")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("zh-Hant"), "zh-TW")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("zh-HK"), "zh-HK")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("zh-CN"), "zh-CN")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("ko-KR"), "ko")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("fr-FR"), "fr")
        self.assertEqual(subtitle_lang.normalize_subtitle_lang("xx"), "en")

    def test_update_and_timeout_reset(self):
        with mock.patch("server.subtitle_lang.time.time", return_value=1000.0):
            st = subtitle_lang.update_subtitle_lang_state({"lang": "ko"})
            self.assertEqual(st["lang"], "ko")
            self.assertEqual(st["lang_name_ja"], "韓国語")
            self.assertTrue(st["is_switched"])
            self.assertEqual(st["remaining_sec"], subtitle_lang.SUBTITLE_TIMEOUT_SEC)
            self.assertEqual(st["last_foreign_comment_ts"], 1000.0)

        with mock.patch("server.subtitle_lang.time.time", return_value=1100.0):
            st = subtitle_lang.get_current_subtitle_lang_status()
            self.assertEqual(st["lang"], "ko")
            self.assertTrue(st["is_switched"])
            self.assertEqual(st["remaining_sec"], subtitle_lang.SUBTITLE_TIMEOUT_SEC - 100)

        with mock.patch(
            "server.subtitle_lang.time.time",
            return_value=1000.0 + subtitle_lang.SUBTITLE_TIMEOUT_SEC,
        ):
            st = subtitle_lang.get_current_subtitle_lang_status()
            self.assertEqual(st["lang"], "en")
            self.assertFalse(st["is_switched"])
            self.assertEqual(st["remaining_sec"], 0)
            self.assertEqual(st["last_foreign_comment_ts"], 0.0)

    def test_explicit_reset(self):
        subtitle_lang.update_subtitle_lang_state({"lang": "fr"})
        st = subtitle_lang.update_subtitle_lang_state({"reset": True})
        self.assertEqual(st["lang"], "en")
        self.assertFalse(st["is_switched"])
        self.assertEqual(st["remaining_sec"], 0)
        self.assertEqual(st["last_foreign_comment_ts"], 0.0)


class TestHandlerOptionalTranslatorAndSubtitleLang(unittest.TestCase):
    BASE_MODULES = (
        "__init__.py",
        "config.py",
        "event_log.py",
        "extensions.py",
        "feed_log.py",
        "handler.py",
        "twitch_api.py",
        "voicevox.py",
        "youtube_chat.py",
    )

    def _run_in_isolated_server(self, extra_modules, script):
        with tempfile.TemporaryDirectory() as tmp:
            fake_base = os.path.join(tmp, "pkg")
            fake_server = os.path.join(fake_base, "server")
            os.makedirs(fake_server)
            for name in self.BASE_MODULES + tuple(extra_modules):
                shutil.copy2(
                    os.path.join(REPO_ROOT, "server", name),
                    os.path.join(fake_server, name),
                )
            env = os.environ.copy()
            env["OBS_WIDGETS_EXTENSIONS_DIR"] = ""
            env.pop("OBS_WIDGETS_SERVER_PROFILE", None)
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=fake_base,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr

    def test_without_translator_and_subtitle_lang(self):
        """webcanvas / pngtuber 相当: translator.py も subtitle_lang.py も無い構成で起動し、該当APIのみ404縮退する"""
        script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def req(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

t_code, t_body = req("POST", "/api/translate", {"text": "hello"})
sg_code, sg_body = req("GET", "/api/subtitle/lang")
sp_code, sp_body = req("POST", "/api/subtitle/lang", {"lang": "ko"})
cfg_code, _ = req("GET", "/api/config")

srv.shutdown()
srv.server_close()

print(json.dumps({
    "translator_enabled": handler._TRANSLATOR_ENABLED,
    "subtitle_lang_enabled": handler._SUBTITLE_LANG_ENABLED,
    "t_code": t_code, "t_body": t_body,
    "sg_code": sg_code, "sg_body": sg_body,
    "sp_code": sp_code, "sp_body": sp_body,
    "cfg_code": cfg_code,
}))
"""
        data, stderr = self._run_in_isolated_server((), script)
        self.assertFalse(data["translator_enabled"])
        self.assertFalse(data["subtitle_lang_enabled"])
        self.assertEqual(data["t_code"], 404)
        self.assertFalse(data["t_body"]["is_translated"])
        self.assertIn("error", data["t_body"])
        self.assertEqual(data["sg_code"], 404)
        self.assertIn("error", data["sg_body"])
        self.assertEqual(data["sp_code"], 404)
        self.assertIn("error", data["sp_body"])
        self.assertEqual(data["cfg_code"], 200)
        self.assertNotIn("import に失敗しました", stderr)

    def test_with_subtitle_lang_only_without_translator(self):
        """voicecommand 相当: subtitle_lang.py のみ同梱、translator.py 無しで字幕言語APIが動作し翻訳APIのみ404縮退する"""
        script = r"""
import json, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from server import handler

srv = ThreadingHTTPServer(("127.0.0.1", 0), handler.UnifiedWidgetHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

def req(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

t_code, t_body = req("POST", "/api/translate", {"text": "hello"})
sp_code, sp_body = req("POST", "/api/subtitle/lang", {"lang": "ko"})
sg_code, sg_body = req("GET", "/api/subtitle/lang")

srv.shutdown()
srv.server_close()

print(json.dumps({
    "translator_enabled": handler._TRANSLATOR_ENABLED,
    "subtitle_lang_enabled": handler._SUBTITLE_LANG_ENABLED,
    "t_code": t_code, "t_body": t_body,
    "sp_code": sp_code, "sp_body": sp_body,
    "sg_code": sg_code, "sg_body": sg_body,
}))
"""
        data, stderr = self._run_in_isolated_server(("subtitle_lang.py",), script)
        self.assertFalse(data["translator_enabled"])
        self.assertTrue(data["subtitle_lang_enabled"])
        self.assertEqual(data["t_code"], 404)
        self.assertFalse(data["t_body"]["is_translated"])
        self.assertEqual(data["sp_code"], 200)
        self.assertEqual(data["sp_body"]["lang"], "ko")
        self.assertEqual(data["sg_code"], 200)
        self.assertEqual(data["sg_body"]["lang"], "ko")
        self.assertNotIn("import に失敗しました", stderr)


if __name__ == "__main__":
    unittest.main()
