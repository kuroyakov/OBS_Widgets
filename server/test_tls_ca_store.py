#!/usr/bin/env python3
"""
TLS CA 証明書ストア確保 (Issue #58) の単体・HTTP テスト:
- build_outbound_ssl_context() の verify_mode / check_hostname 検証 (S-07)
- certifi 不在時の OS 候補パスへのフォールバック
- 存在しないパス・空ファイル・破損 PEM・権限エラーのスキップと次候補採用
- 全候補不在時の ca_count == 0 / ok == False (検証無効化へは絶対に落とさない)
- 起動時セルフチェックの stderr 警告と /api/twitch/eventsub/subscribe 502 の code 露出
"""

import builtins
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402


class _EmptySSLContext:
    """既定ストアが空の環境（macOS の素の Python 等）を模擬するテスト用 SSLContext ラッパー。"""

    def __init__(self):
        self._inner = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._inner.verify_mode = ssl.CERT_REQUIRED
        self._inner.check_hostname = True

    @property
    def verify_mode(self):
        return self._inner.verify_mode

    @verify_mode.setter
    def verify_mode(self, value):
        self._inner.verify_mode = value

    @property
    def check_hostname(self):
        return self._inner.check_hostname

    @check_hostname.setter
    def check_hostname(self, value):
        self._inner.check_hostname = value

    def load_verify_locations(self, cafile=None, capath=None, cadata=None):
        return self._inner.load_verify_locations(cafile=cafile, capath=capath, cadata=cadata)

    def get_ca_certs(self, binary_form=False):
        return self._inner.get_ca_certs(binary_form=binary_form)


def _create_temp_ca_pem(tmpdir):
    """テスト用の有効な自己署名 CA 証明書 PEM を生成する。"""
    pem_path = os.path.join(tmpdir, "test-ca.pem")
    key_path = os.path.join(tmpdir, "test-ca.key")
    openssl_bin = shutil.which("openssl")
    if openssl_bin:
        subprocess.run(
            [
                openssl_bin,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key_path,
                "-out",
                pem_path,
                "-days",
                "1",
                "-subj",
                "/CN=OBS-Widgets-Test-CA",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return pem_path

    # openssl が無い環境ではシステムの CA バンドルをコピーして代用
    for sys_ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
        if os.path.isfile(sys_ca) and os.path.getsize(sys_ca) > 0:
            shutil.copyfile(sys_ca, pem_path)
            return pem_path
    raise unittest.SkipTest("neither openssl nor system CA bundle available")


class TestBuildOutboundSslContext(unittest.TestCase):
    def test_default_context_enforces_cert_required_and_check_hostname(self):
        ctx, info = server_config.build_outbound_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertIsInstance(info["ok"], bool)
        self.assertIsInstance(info["ca_count"], int)
        self.assertIn(info["source"].split(":", 1)[0], ("system_default", "certifi", "os_bundle", "none"))

    def test_fallback_to_os_bundle_when_certifi_absent_and_skips_bad_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing.pem")
            empty_path = os.path.join(tmpdir, "empty.pem")
            with open(empty_path, "w", encoding="utf-8") as f:
                f.write("")
            corrupt_path = os.path.join(tmpdir, "corrupt.pem")
            with open(corrupt_path, "w", encoding="utf-8") as f:
                f.write("-----BEGIN CERTIFICATE-----\nINVALID_BASE64\n-----END CERTIFICATE-----\n")
            unreadable_path = os.path.join(tmpdir, "unreadable.pem")
            with open(unreadable_path, "w", encoding="utf-8") as f:
                f.write("dummy")
            valid_ca_path = _create_temp_ca_pem(tmpdir)

            real_import = builtins.__import__

            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "certifi":
                    raise ImportError("No module named 'certifi'")
                return real_import(name, globals, locals, fromlist, level)

            fake_ctx = _EmptySSLContext()
            real_load = fake_ctx.load_verify_locations

            def fake_load(cafile=None, capath=None, cadata=None):
                if cafile == unreadable_path:
                    raise PermissionError("permission denied")
                return real_load(cafile=cafile, capath=capath, cadata=cadata)

            fake_ctx.load_verify_locations = fake_load

            with (
                mock.patch.object(ssl, "create_default_context", return_value=fake_ctx),
                mock.patch("builtins.__import__", side_effect=fake_import),
            ):
                ctx, info = server_config.build_outbound_ssl_context(
                    platform="darwin",
                    candidates=(missing_path, empty_path, corrupt_path, unreadable_path, valid_ca_path),
                )

            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
            self.assertTrue(info["ok"])
            self.assertGreater(info["ca_count"], 0)
            self.assertEqual(info["source"], f"os_bundle:{valid_ca_path}")
            self.assertIsNone(info["error"])

    def test_all_candidates_unavailable_returns_ok_false_and_keeps_verification_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = os.path.join(tmpdir, "missing.pem")
            empty_path = os.path.join(tmpdir, "empty.pem")
            with open(empty_path, "w", encoding="utf-8") as f:
                f.write("")

            real_import = builtins.__import__

            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "certifi":
                    raise ImportError("No module named 'certifi'")
                return real_import(name, globals, locals, fromlist, level)

            with (
                mock.patch.object(ssl, "create_default_context", return_value=_EmptySSLContext()),
                mock.patch("builtins.__import__", side_effect=fake_import),
            ):
                ctx, info = server_config.build_outbound_ssl_context(
                    platform="darwin",
                    candidates=(missing_path, empty_path),
                )

            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
            self.assertFalse(info["ok"])
            self.assertEqual(info["ca_count"], 0)
            self.assertEqual(info["source"], "none")
            self.assertTrue(info["error"])


class TestTlsStoreStartupAndSubscribeApi(unittest.TestCase):
    def test_startup_warning_printed_to_stderr_when_ca_store_empty(self):
        script = (
            "import importlib.util, io, os\n"
            "os.closerange = lambda *a, **k: None\n"
            "spec = importlib.util.spec_from_file_location('server_entry', 'server.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "buf = io.StringIO()\n"
            "warned = mod._warn_if_tls_store_unavailable("
            "{'ok': False, 'ca_count': 0, 'source': 'none', 'error': 'simulated empty'}, stream=buf)\n"
            "assert warned is True\n"
            "text = buf.getvalue()\n"
            "assert '[TLS WARNING]' in text and 'ca_count=0' in text, text\n"
            "ok_buf = io.StringIO()\n"
            "assert mod._warn_if_tls_store_unavailable({'ok': True, 'ca_count': 10}, stream=ok_buf) is False\n"
            "assert ok_buf.getvalue() == ''\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_subscribe_502_exposes_tls_store_unavailable_code_only_when_store_down(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "twitch_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"client_id": "cid", "access_token": "tok"}, f)

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    mock.patch.object(server_config, "CONFIG_FILE", cfg_path),
                    mock.patch.object(
                        server_handler,
                        "validate_twitch_token",
                        return_value={"valid": False, "error": "CERTIFICATE_VERIFY_FAILED"},
                    ),
                ):
                    server_config._CONFIG_CACHE["key"] = None
                    server_config._CONFIG_CACHE["value"] = None

                    # 1. TLS ストア不可時: code="tls_store_unavailable" と tls_store_ok=False が返る
                    with mock.patch.object(
                        server_config,
                        "SSL_CONTEXT_INFO",
                        {"ok": False, "ca_count": 0, "source": "none", "error": "empty"},
                    ):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        conn.request(
                            "POST",
                            "/api/twitch/eventsub/subscribe",
                            body=json.dumps({"session_id": "sess-1"}),
                            headers={"Content-Type": "application/json"},
                        )
                        resp = conn.getresponse()
                        body = json.loads(resp.read())
                        conn.close()
                        self.assertEqual(resp.status, 502)
                        self.assertEqual(body.get("code"), "tls_store_unavailable")
                        self.assertIs(body.get("tls_store_ok"), False)

                    # 2. TLS ストア正常時の通常 502: tls_store_unavailable は付かない
                    with mock.patch.object(
                        server_config,
                        "SSL_CONTEXT_INFO",
                        {"ok": True, "ca_count": 100, "source": "system_default", "error": None},
                    ):
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        conn.request(
                            "POST",
                            "/api/twitch/eventsub/subscribe",
                            body=json.dumps({"session_id": "sess-2"}),
                            headers={"Content-Type": "application/json"},
                        )
                        resp = conn.getresponse()
                        body = json.loads(resp.read())
                        conn.close()
                        self.assertEqual(resp.status, 502)
                        self.assertNotIn("code", body)
            finally:
                server_config._CONFIG_CACHE["key"] = None
                server_config._CONFIG_CACHE["value"] = None
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
