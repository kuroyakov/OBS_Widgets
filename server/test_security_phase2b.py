#!/usr/bin/env python3
"""
セキュリティ Phase 2b (Issue #52, VULN-04 / VULN-05) の単体・HTTP テスト:
- 静的配信のホワイトリスト化（ALLOWED_STATIC_DIRS + 拡張静的マウント）
- ルート直下ファイル・許可外セグメント・NTFS 別名 (::$DATA / 末尾ドット・空白)・制御文字・symlink 脱出の遮断
- 全ウィジェット主要ページ・拡張マウント・例外ルートの正常配信
- /obs-ws トンネルの Origin 厳密検証 (_is_valid_obs_ws_origin)
- /obs-ws 同時接続上限 (6本、503 即切断、finally 解放)・無通信アイドルタイムアウト・ソケットタイムアウト切替
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402


def _raw_http_request(port, method, path, body=b"", headers=None, host_header=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        req_headers = dict(headers or {})
        if host_header is not None:
            req_headers["Host"] = host_header
        conn.putrequest(method, path, skip_host=(host_header is not None))
        for k, v in req_headers.items():
            conn.putheader(k, v)
        if body is not None and "Content-Length" not in {k.title() for k in req_headers}:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or b"")
        resp = conn.getresponse()
        raw_body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw_body
    finally:
        conn.close()


def _send_raw_ws_upgrade(port, path="/obs-ws", origin=None):
    """生のソケットで WebSocket アップグレード要求を送り、(status_line, sock) を返す。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    sock.sendall(req)

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    header_part = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
    status_line = header_part.splitlines()[0] if header_part else ""
    return status_line, sock


class _DummyObsWsUpstream:
    """/obs-ws トンネルの中継先となる最小 TCP/WS ダミーサーバー。"""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(32)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self._conns = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop:
            try:
                self._srv.settimeout(0.2)
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
            )
            while not self._stop:
                conn.settimeout(0.2)
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except OSError:
                    pass
            self._conns.clear()


class TestStaticWhitelistPhase2b(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ext_dir = os.path.join(self.tmpdir.name, "ext_controller")
        os.makedirs(self.ext_dir, exist_ok=True)
        with open(os.path.join(self.ext_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>controller</html>")
        with open(os.path.join(self.ext_dir, "twitch_config.json"), "w", encoding="utf-8") as f:
            f.write('{"secret":"nope"}')
        with open(os.path.join(self.ext_dir, "secret.py"), "w", encoding="utf-8") as f:
            f.write("print('secret')\n")
        with open(os.path.join(self.ext_dir, ".hidden.txt"), "w", encoding="utf-8") as f:
            f.write("hidden\n")

        with open(os.path.join(self.ext_dir, "env-config.local.js"), "w", encoding="utf-8") as f:
            f.write("window.ENV = { secret: true };\n")
        with open(os.path.join(self.ext_dir, "launcher_config.json.bak"), "w", encoding="utf-8") as f:
            f.write("{}\n")

        self.outside_secret = os.path.join(self.tmpdir.name, "outside_secret.txt")
        with open(self.outside_secret, "w", encoding="utf-8") as f:
            f.write("OUTSIDE_SECRET")

        # symlink 脱出 (拡張マウント外を指す)
        self.escape_link = os.path.join(self.ext_dir, "escape.txt")
        os.symlink(self.outside_secret, self.escape_link)
        # symlink でマウント内の隠しファイル / 機密名 / 禁止拡張子 / env-config.local.js を指す
        os.symlink(os.path.join(self.ext_dir, ".hidden.txt"), os.path.join(self.ext_dir, "alias_hidden.txt"))
        os.symlink(os.path.join(self.ext_dir, "twitch_config.json"), os.path.join(self.ext_dir, "alias_cfg.txt"))
        os.symlink(os.path.join(self.ext_dir, "secret.py"), os.path.join(self.ext_dir, "alias_py.txt"))
        os.symlink(os.path.join(self.ext_dir, "env-config.local.js"), os.path.join(self.ext_dir, "alias_env.js"))

        self.patch_mounts = mock.patch.object(
            server_handler,
            "_EXTENSION_STATIC_MOUNTS",
            {"Controller": os.path.realpath(self.ext_dir)},
        )
        self.patch_mounts.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.patch_mounts.stop()
        self.tmpdir.cleanup()

    def test_blocked_static_paths_return_403_on_get_and_head(self):
        blocked = (
            # ルート直下ファイル
            "/README.md",
            "/SPEC.md",
            "/server.py",
            "/twitch_config.example.json",
            "/twitch_config.json",
            "/twitch_config.json.corrupt",
            "/twitch_config.json.bak",
            "/twitch_config.json.tmp.1",
            # 許可外ディレクトリ・大文字小文字不一致
            "/server/config.py",
            "/server/README.md",
            "/docs/BRIEF_CHECKLIST.md",
            "/tools/e2e_smoke.py",
            "/jimakuChan-auto-lang/manifest.json",
            "/twitch-audio-panner/manifest.json",
            "/unknown_dir/index.html",
            "/clock/Date.html",
            # NTFS ADS (::$DATA) / コロン
            "/twitch_config.json::$DATA",
            "/Clock/Date.html::$DATA",
            "/Clock/foo:bar.html",
            # 末尾ドット・空白
            "/twitch_config.json.",
            "/twitch_config.json%20",
            "/Clock/Date.html.",
            "/Clock/Date.html%20",
            "/Clock./Date.html",
            "/Clock%20/Date.html",
            # 制御文字
            "/Clock/Date.html%00",
            "/Clock/Date.html%0a",
            "/Clock/Date.html%7f",
            # symlink 脱出およびマウント内禁止ターゲットへの symlink
            "/Controller/escape.txt",
            "/Controller/alias_hidden.txt",
            "/Controller/alias_cfg.txt",
            "/Controller/alias_py.txt",
            "/Controller/twitch_config.json",
            "/Controller/launcher_config.json.bak",
            "/Controller/.hidden.txt",
        )
        for path in blocked:
            for method in ("GET", "HEAD"):
                status, _, body = _raw_http_request(self.port, method, path)
                self.assertEqual(status, 403, f"{method} {path} should be 403, got {status}")
                if method == "HEAD":
                    self.assertEqual(body, b"")

    def test_non_local_client_blocked_for_env_config_local_js(self):
        with mock.patch.object(
            server_handler.UnifiedWidgetHandler, "_client_is_local", return_value=False
        ):
            for path in ("/shared/env-config.local.js", "/Controller/alias_env.js"):
                for method in ("GET", "HEAD"):
                    status, _, _ = _raw_http_request(self.port, method, path)
                    self.assertEqual(status, 403, f"{method} {path} should be 403, got {status}")

    def test_translate_path_defense_in_depth_blocks_disallowed_paths(self):
        dummy_handler = server_handler.UnifiedWidgetHandler.__new__(
            server_handler.UnifiedWidgetHandler
        )
        for path in ("/README.md", "/Controller/escape.txt", "/Clock/Date.html::$DATA"):
            resolved = dummy_handler.translate_path(path)
            self.assertFalse(os.path.exists(resolved), f"{path} resolved to existing file {resolved}")

    def test_allowed_widgets_extension_mount_and_exception_routes_succeed(self):
        allowed_pages = (
            "/Clock/Date.html",
            "/CommentViewer/index.html",
            "/EndRoll/index.html",
            "/KeyboardVisualizer/keyboard.html",
            "/PngTuber/index.html",
            "/PngTuber/settings.html",
            "/StampAlert_Twitch/stamp-alert.html",
            "/Ticker/info.html",
            "/VoiceCommand/settings.html",
            "/WebCanvas/overlay.html",
            "/WebCanvas/pad.html",
            "/shared/env-config.js",
            "/Controller/index.html",
            "/Controller/sub/../index.html",
        )
        for path in allowed_pages:
            status, _, body = _raw_http_request(self.port, "GET", path)
            self.assertEqual(status, 200, f"GET {path} should be 200, got {status}")
            self.assertGreater(len(body), 0)
            status_head, _, _ = _raw_http_request(self.port, "HEAD", path)
            self.assertEqual(status_head, 200, f"HEAD {path} should be 200, got {status_head}")

        # 0-C: /Ticker 2/ -> /Ticker/ フォールバック (GET)
        st, _, body = _raw_http_request(self.port, "GET", "/Ticker%202/info.html")
        self.assertEqual(st, 200)
        self.assertGreater(len(body), 0)

        # 例外ルート: favicon 204 / リダイレクト 302 / onesdk.js スタブ 200 / API 200
        for p204 in ("/favicon.ico", "/.well-known/apple-app-site-association"):
            st, _, _ = _raw_http_request(self.port, "GET", p204)
            self.assertEqual(st, 204)
        for p302 in ("/", "/pad.html"):
            st, _, _ = _raw_http_request(self.port, "GET", p302)
            self.assertEqual(st, 302)
        st, _, body = _raw_http_request(self.port, "GET", "/WebCanvas/onesdk.js")
        self.assertEqual(st, 200)
        self.assertIn(b"OneSDK stub", body)
        st, _, _ = _raw_http_request(self.port, "GET", "/api/config")
        self.assertEqual(st, 200)


class TestObsWsTunnelProtectionPhase2b(unittest.TestCase):
    def setUp(self):
        self.upstream = _DummyObsWsUpstream()
        self.patch_host = mock.patch.object(
            server_handler, "OBS_WS_UPSTREAM_HOST", "127.0.0.1"
        )
        self.patch_port = mock.patch.object(
            server_handler, "OBS_WS_UPSTREAM_PORT", self.upstream.port
        )
        self.patch_sem = mock.patch.object(
            server_handler,
            "_OBS_WS_TUNNEL_SEMAPHORE",
            threading.Semaphore(server_handler.OBS_WS_MAX_TUNNELS),
        )
        self.patch_host.start()
        self.patch_port.start()
        self.patch_sem.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_handler.UnifiedWidgetHandler)
        self.server.obs_ws_tunnel_enabled = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.upstream.close()
        self.patch_sem.stop()
        self.patch_port.stop()
        self.patch_host.stop()

    def test_handler_socket_timeout_is_15_seconds(self):
        self.assertEqual(server_handler.UnifiedWidgetHandler.timeout, 15)

    def test_obs_ws_origin_validation_rejects_invalid_and_allows_valid(self):
        rejected_origins = (
            None,
            "",
            "null",
            "NULL",
            f"http://localhost:{self.port}",
            f"ws://localhost:{self.port}",
            f"https://evil.example:{self.port}",
            "https://sayonari.github.io",
            "https://www.sayonari.com",
            f"https://localhost.evil.example:{self.port}",
            f"https://evil.local.evil.com:{self.port}",
            f"https://8.8.8.8:{self.port}",
            f"https://localhost:{self.port + 1}",
            "https://localhost",
        )
        for origin in rejected_origins:
            status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
            sock.close()
            self.assertIn("403", status_line, f"Origin={origin!r} should be 403, got {status_line!r}")

        allowed_origins = (
            f"https://localhost:{self.port}",
            f"https://127.0.0.1:{self.port}",
            f"https://[::1]:{self.port}",
            f"https://my-mac.local:{self.port}",
            f"https://{'.'.join(('192', '168', '1', '50'))}:{self.port}",
            f"https://{'.'.join(('10', '0', '0', '5'))}:{self.port}",
            f"https://{'.'.join(('172', '16', '0', '5'))}:{self.port}",
            f"https://169.254.1.10:{self.port}",
        )
        for origin in allowed_origins:
            status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
            try:
                self.assertIn("101", status_line, f"Origin={origin!r} should succeed, got {status_line!r}")
                sock.sendall(b"ping")
                self.assertEqual(sock.recv(16), b"ping")
            finally:
                sock.close()

    def test_obs_ws_concurrency_limit_6_returns_503_and_releases_on_close(self):
        origin = f"https://localhost:{self.port}"
        open_socks = []
        try:
            for i in range(6):
                status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
                self.assertIn("101", status_line, f"Tunnel #{i+1} should succeed")
                open_socks.append(sock)

            # 7本目は 503 で即切断されること
            status_7, sock_7 = _send_raw_ws_upgrade(self.port, origin=origin)
            sock_7.close()
            self.assertIn("503", status_7, f"7th tunnel should be 503, got {status_7!r}")

            # 通常 API は影響を受けず 200 を返すこと
            api_status, _, _ = _raw_http_request(self.port, "GET", "/api/config")
            self.assertEqual(api_status, 200)

            # 1本閉じれば再び接続できること (finally 解放の確認)
            open_socks.pop().close()
            time.sleep(0.1)
            status_reopen, sock_reopen = _send_raw_ws_upgrade(self.port, origin=origin)
            try:
                self.assertIn("101", status_reopen, f"Reopened tunnel should succeed, got {status_reopen!r}")
            finally:
                sock_reopen.close()
        finally:
            for s in open_socks:
                try:
                    s.close()
                except OSError:
                    pass

    def test_obs_ws_upstream_failure_releases_semaphore(self):
        origin = f"https://localhost:{self.port}"
        with mock.patch.object(server_handler, "OBS_WS_UPSTREAM_PORT", 1):
            for _ in range(server_handler.OBS_WS_MAX_TUNNELS + 2):
                status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
                sock.close()
                self.assertIn("502", status_line)

        # すべて 502 で解放されているため正規上流へ 101 で接続できる
        status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
        try:
            self.assertIn("101", status_line)
        finally:
            sock.close()

    def test_obs_ws_idle_timeout_disconnects_and_switches_socket_timeout(self):
        origin = f"https://localhost:{self.port}"
        recorded_timeouts = []
        orig_select = server_handler.select.select

        def spy_select(rlist, wlist, xlist, timeout=None):
            for sock_obj in rlist:
                if hasattr(sock_obj, "gettimeout"):
                    recorded_timeouts.append(sock_obj.gettimeout())
            return orig_select(rlist, wlist, xlist, timeout)

        with (
            mock.patch.object(server_handler, "OBS_WS_IDLE_TIMEOUT", 0.2),
            mock.patch.object(server_handler.select, "select", side_effect=spy_select),
        ):
            status_line, sock = _send_raw_ws_upgrade(self.port, origin=origin)
            try:
                self.assertIn("101", status_line)
                sock.settimeout(3)
                # 無通信で 0.2 秒待つとサーバー側が切断する (EOF)
                data = sock.recv(1024)
                self.assertEqual(data, b"")
            finally:
                sock.close()

        self.assertIn(server_handler.OBS_WS_SOCKET_TIMEOUT, recorded_timeouts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
