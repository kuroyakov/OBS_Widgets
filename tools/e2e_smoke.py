#!/usr/bin/env python3
"""リリースパッケージ単独スモーク E2E (Issue #43 Phase 1)。

使い方:
    python3 tools/e2e_smoke.py
    python3 tools/e2e_smoke.py --package clock --package webcanvas
    python3 tools/e2e_smoke.py --package clock --zip dist/clock-v1.0.0.zip --artifacts-dir e2e-artifacts

設計:
- `tools/build_package.py` でビルドした ZIP (または --zip 指定の既存 ZIP) を
  リポジトリ作業ツリーとは無関係な一時ディレクトリに展開してテストする。
- README の最小セットアップ (example 設定ファイルのコピー等) を展開先で再現する。
- サーバー同梱パッケージは展開先で `python3 server.py` を起動して疎通確認する。
- 外部ネットワークリクエストはすべて遮断し、外部サービスへ接続しない。
- 初期化に obs-websocket が必要なパッケージのみ、最小の偽 obs-websocket v5 を立てる。
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_package  # noqa: E402

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080
OBS_WS_HOST = "127.0.0.1"
OBS_WS_PORT = 4455

# コンソールエラー許容リスト (Issue #43: パッケージ名 + パターン + 理由の明示リスト方式)
# 無条件の握りつぶしは禁止。
CONSOLE_ERROR_ALLOWLIST = {
    "clock": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 Google Fonts リクエストをテスト側で意図的に遮断しているため",
        ),
    ],
    "endroll": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 Google Fonts リクエストをテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_FILE_NOT_FOUND"),
            "わんコメ本体が提供する ../../preset/__origin/js/onesdk.js は単独展開では不在のため",
        ),
        (
            re.compile(r"OneSDK が見つかりません"),
            "わんコメ OneSDK 未配置時の EndRoll 側の想定内エラーログ",
        ),
    ],
    "stampalert-twitch": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 Twitch API/CDN リクエストをテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"WebSocket connection to 'wss://irc-ws\.chat\.twitch\.tv"),
            "外部 Twitch IRC WebSocket をテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_CONNECTION_REFUSED"),
            "サーバー不要パッケージであり、任意の http://localhost:8080/api/config 接続が未起動のため拒否される想定挙動",
        ),
    ],
    "webcanvas": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 CDN (obs-websocket-js) リクエストをテスト側で意図的に遮断しているため",
        ),
    ],
    "keyboard": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 CDN (obs-websocket-js) リクエストをテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_FILE_NOT_FOUND"),
            "ユーザー任意素材の KeyboardVisualizer/keyboard.png 未配置時フォールバック (keyboard.example.png へ切替)",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_CONNECTION_REFUSED"),
            "サーバー不要パッケージであり、任意の http://localhost:8080/api/* 接続が未起動のため拒否される想定挙動",
        ),
    ],
    "ticker": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 Google Fonts / CDN (obs-websocket-js) リクエストをテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_CONNECTION_REFUSED"),
            "サーバー不要パッケージであり、任意の http://localhost:8080/api/* 接続が未起動のため拒否される想定挙動",
        ),
    ],
    "pngtuber": [
        (
            re.compile(r"Failed to load resource: the server responded with a status of 404"),
            "初回セットアップ前（アバター画像未アップロード状態）では PngTuber/images/mouth_*.png が未配置のため",
        ),
    ],
    "commentviewer": [
        (
            re.compile(r"Failed to load resource: net::ERR_FAILED"),
            "外部 CDN (obs-websocket-js) リクエストをテスト側で意図的に遮断しているため",
        ),
        (
            re.compile(r"Failed to load resource: net::ERR_CONNECTION_REFUSED"),
            "任意のローカル VOICEVOX Engine (127.0.0.1:50021) がテスト環境では未起動のため",
        ),
        (
            re.compile(r"Failed to load resource: the server responded with a status of 401"),
            "twitch_config.example.json の未認可状態では /api/twitch/eventsub/subscribe が 401 を返す想定挙動",
        ),
    ],
}

# ローカル file:// / localhost リソースの読み込み失敗許容リスト
LOCAL_RESOURCE_FAILURE_ALLOWLIST = {
    "endroll": [
        (
            re.compile(r"preset/__origin/js/onesdk\.js$"),
            "わんコメ本体が提供する onesdk.js は単独展開では不在のため",
        ),
    ],
    "stampalert-twitch": [
        (
            re.compile(r"^http://localhost:8080/api/config$"),
            "サーバー不要パッケージのため localhost:8080 は未起動",
        ),
    ],
    "keyboard": [
        (
            re.compile(r"KeyboardVisualizer/keyboard\.png$"),
            "ユーザー任意素材の keyboard.png 未配置時は onerror で keyboard.example.png にフォールバックするため",
        ),
        (
            re.compile(r"^http://localhost:8080/api/(config|heartbeat)(\?.*)?$"),
            "サーバー不要パッケージのため localhost:8080 は未起動",
        ),
    ],
    "ticker": [
        (
            re.compile(r"^http://localhost:8080/api/(config|launcher/action)$"),
            "サーバー不要パッケージのため localhost:8080 は未起動",
        ),
    ],
    "commentviewer": [
        (
            re.compile(r"^http://127\.0\.0\.1:50021/"),
            "任意のローカル VOICEVOX Engine がテスト環境では未起動のため",
        ),
    ],
}

# サーバー応答 (>= 400) の明示許容リスト: (HTTP status, URL regex, reason)
HTTP_ERROR_ALLOWLIST = {
    "pngtuber": [
        (
            404,
            re.compile(r"/PngTuber/images/mouth_(open|closed)_eye_(open|closed)\.png$"),
            "初回セットアップ前（アバター画像未アップロード状態）では未配置のため",
        ),
    ],
    "commentviewer": [
        (
            401,
            re.compile(r"/api/twitch/eventsub/subscribe$"),
            "twitch_config.example.json の未認可状態では 401 Unauthorized を返す想定挙動のため",
        ),
    ],
}

# パッケージ別のエントリーポイント・確認条件
PACKAGE_SPECS = {
    "clock": {
        "server": False,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "Clock/Date.html",
                "selectors": ["#date-text", "#day-text", "#h1", "#m1"],
            },
        ],
    },
    "endroll": {
        "server": False,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "EndRoll/index.html",
                "selectors": ["#viewport", "#credits-track", "#ui-closing"],
            },
        ],
    },
    "stampalert-twitch": {
        "server": False,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "StampAlert_Twitch/stamp-alert.html",
                "selectors": ["#stage", "#monitor-panel", "#conn-status"],
            },
        ],
    },
    "twitch-audio-panner": {
        "server": False,
        "mock_obs_ws": False,
        "extension_dir": "twitch-audio-panner",
        "extension_has_sw": True,
        "pages": [
            {
                "path": "popup.html",
                "is_extension_page": True,
                "selectors": [".brand", "#mixerRack", "#masterVol"],
            },
        ],
    },
    "webcanvas": {
        "server": True,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "WebCanvas/overlay.html",
                "selectors": ["#canvas"],
            },
            {
                "path": "WebCanvas/pad.html",
                "selectors": ["#toolbar", "#draw-canvas", "#select-source"],
            },
        ],
    },
    "keyboard": {
        "server": False,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "KeyboardVisualizer/keyboard.html",
                "selectors": ["#keyboard-wrapper", "#white-keys", "#black-keys", "#midi-status"],
            },
        ],
    },
    "ticker": {
        "server": False,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "Ticker/info.html",
                "selectors": ["#ticker-wrap", ".ticker-badge", "#ticker-item"],
            },
        ],
    },
    "pngtuber": {
        "server": True,
        "mock_obs_ws": True,
        "pages": [
            {
                "path": "PngTuber/index.html",
                "selectors": ["#avatar-stage", "#avatar-canvas"],
            },
            {
                "path": "PngTuber/settings.html",
                "selectors": ["#conn-badge", "#avatar-select", "#mic-select"],
            },
        ],
    },
    "commentviewer": {
        "server": True,
        "mock_obs_ws": False,
        "pages": [
            {
                "path": "CommentViewer/index.html",
                "selectors": ["#viewer-stage", "#hud-panel", "#sel-theme"],
            },
        ],
    },
    "voicecommand": {
        "server": True,
        "mock_obs_ws": True,
        "extension_dir": "jimakuChan-auto-lang",
        "extension_has_sw": False,
        "pages": [
            {
                "path": "VoiceCommand/settings.html",
                "selectors": ["#conn-badge", "#cfg-enabled", "#cfg-wake-words"],
            },
        ],
    },
}


class MinimalMockObsWsServer:
    """OBS WebSocket v5 の最小モックサーバー (RFC 6455 標準ライブラリ実装)。

    Hello (op=0) -> Identify (op=1) -> Identified (op=2) と
    Request (op=6) -> RequestResponse (op=7) の最小応答のみを行う。
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host=OBS_WS_HOST, port=OBS_WS_PORT):
        self.host = host
        self.port = port
        self._sock = None
        self._stop = threading.Event()
        self._thread = None
        self._conns = []
        self._lock = threading.Lock()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except OSError:
                    pass
            self._conns.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            t.start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(1.0)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            headers_text = req.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
            ws_key = ""
            for line in headers_text.splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    ws_key = line.split(":", 1)[1].strip()
                    break
            if not ws_key:
                return
            accept = base64.b64encode(
                hashlib.sha1((ws_key + self.GUID).encode("ascii")).digest()
            ).decode("ascii")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            conn.sendall(resp)

            self._send_json(conn, {
                "op": 0,
                "d": {
                    "obsWebSocketVersion": "5.4.0",
                    "rpcVersion": 1,
                },
            })

            while not self._stop.is_set():
                frame = self._recv_frame(conn)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # Close
                    break
                if opcode == 0x9:  # Ping
                    self._send_frame(conn, 0xA, payload)
                    continue
                if opcode != 0x1:  # Text only
                    continue
                msg = json.loads(payload.decode("utf-8"))
                op = msg.get("op")
                d = msg.get("d") or {}
                if op == 1:  # Identify
                    self._send_json(conn, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
                elif op == 6:  # Request
                    req_type = d.get("requestType", "")
                    req_id = d.get("requestId", "")
                    self._send_json(conn, {
                        "op": 7,
                        "d": {
                            "requestType": req_type,
                            "requestId": req_id,
                            "requestStatus": {"result": True, "code": 100},
                            "responseData": self._default_response(req_type),
                        },
                    })
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _default_response(req_type):
        if req_type == "GetVersion":
            return {"obsVersion": "30.0.0", "obsWebSocketVersion": "5.4.0", "rpcVersion": 1}
        if req_type == "GetInputList":
            return {"inputs": []}
        if req_type == "GetSceneList":
            return {"currentProgramSceneName": "Scene", "scenes": [{"sceneName": "Scene"}]}
        if req_type == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Scene"}
        if req_type == "GetHotkeyList":
            return {"hotkeys": []}
        if req_type == "GetSourceFilterList":
            return {"filters": []}
        return {}

    @staticmethod
    def _recv_exact(conn, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _recv_frame(self, conn):
        while not self._stop.is_set():
            try:
                header = conn.recv(2)
            except socket.timeout:
                continue
            if not header or len(header) < 2:
                return None
            b1, b2 = header[0], header[1]
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                ext = self._recv_exact(conn, 2)
                if ext is None:
                    return None
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(conn, 8)
                if ext is None:
                    return None
                length = struct.unpack("!Q", ext)[0]
            mask_key = self._recv_exact(conn, 4) if masked else b""
            if masked and mask_key is None:
                return None
            data = self._recv_exact(conn, length) if length > 0 else b""
            if data is None:
                return None
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            return opcode, data
        return None

    @staticmethod
    def _send_frame(conn, opcode, payload):
        header = bytearray([0x80 | (opcode & 0x0F)])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", n))
        conn.sendall(bytes(header) + payload)

    def _send_json(self, conn, obj):
        self._send_frame(conn, 0x1, json.dumps(obj).encode("utf-8"))


def _is_allowed_url(url):
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ("file", "chrome-extension", "data", "blob", "about"):
        return True
    if scheme in ("http", "https", "ws", "wss"):
        host = (parsed.hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")
    return False


def _setup_extracted_package(extract_dir):
    """README の最小セットアップ手順を展開先ディレクトリ上で再現する。"""
    example_copies = [
        (extract_dir / "shared" / "env-config.example.local.js", extract_dir / "shared" / "env-config.local.js"),
        (extract_dir / "pngtuber_config.example.json", extract_dir / "pngtuber_config.json"),
        (extract_dir / "twitch_config.example.json", extract_dir / "twitch_config.json"),
        (extract_dir / "voice_command_config.example.json", extract_dir / "voice_command_config.json"),
    ]
    for src, dst in example_copies:
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)


def _wait_for_server(url, proc, timeout_sec=10.0):
    deadline = time.monotonic() + timeout_sec
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server.py exited prematurely with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status < 500:
                    return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
            last_err = e
        except Exception as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"server.py did not become ready at {url} within {timeout_sec}s: {last_err}")


def _is_allowed_console_error(pkg, text):
    for pattern, _ in CONSOLE_ERROR_ALLOWLIST.get(pkg, []):
        if pattern.search(text):
            return True
    return False


def _is_allowed_local_failure(pkg, url):
    for pattern, _ in LOCAL_RESOURCE_FAILURE_ALLOWLIST.get(pkg, []):
        if pattern.search(url):
            return True
    return False


def _is_allowed_http_error(pkg, status, url):
    for allowed_status, pattern, _ in HTTP_ERROR_ALLOWLIST.get(pkg, []):
        if status == allowed_status and pattern.search(url):
            return True
    return False


def _write_artifacts(artifacts_dir, pkg, logs, screenshots):
    if artifacts_dir is None:
        return
    pkg_dir = Path(artifacts_dir) / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for name, content in logs.items():
        (pkg_dir / name).write_text(content, encoding="utf-8")
    for idx, (label, png_bytes) in enumerate(screenshots):
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        (pkg_dir / f"{idx:02d}_{safe_label}.png").write_bytes(png_bytes)


def run_package_smoke(pkg, zip_path, artifacts_dir=None, keep_temp=False, server_port=None):
    spec = PACKAGE_SPECS[pkg]
    effective_port = server_port if server_port is not None else SERVER_PORT
    logs = {
        "console.log": "",
        "pageerror.log": "",
        "network.log": "",
        "server.log": "",
    }
    screenshots = []
    errors = []

    tmp_ctx = tempfile.TemporaryDirectory(prefix=f"obs_e2e_{pkg}_")
    extract_dir = Path(tmp_ctx.name)
    server_proc = None
    server_log_file = None
    obs_mock = None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        top_entries = [p for p in extract_dir.iterdir()]
        if len(top_entries) == 1 and top_entries[0].is_dir():
            pkg_root = top_entries[0]
        else:
            pkg_root = extract_dir

        _setup_extracted_package(pkg_root)

        if spec.get("mock_obs_ws"):
            obs_mock = MinimalMockObsWsServer()
            obs_mock.start()

        if spec.get("server"):
            server_py = pkg_root / "server.py"
            if not server_py.is_file():
                raise RuntimeError(f"server.py not found in extracted package: {pkg_root}")
            server_log_path = extract_dir / "_e2e_server.log"
            server_log_file = open(server_log_path, "w", encoding="utf-8")
            server_env = os.environ.copy()
            if server_port is not None:
                server_env["OBS_WIDGETS_PORT"] = str(server_port)
            else:
                server_env.pop("OBS_WIDGETS_PORT", None)
                server_env.pop("OBS_WIDGETS_TLS_PORT", None)
            server_proc = subprocess.Popen(
                [sys.executable, "server.py"],
                cwd=pkg_root,
                env=server_env,
                stdout=server_log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            first_path = spec["pages"][0]["path"].lstrip("/")
            _wait_for_server(f"http://{SERVER_HOST}:{effective_port}/{first_path}", server_proc)

        console_lines = []
        pageerror_lines = []
        network_lines = []

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ext_rel = spec.get("extension_dir")
            if ext_rel:
                ext_path = str((pkg_root / ext_rel).resolve())
                user_data_dir = str((extract_dir / "_chrome_profile").resolve())
                context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    channel="chromium",
                    headless=True,
                    args=[
                        f"--disable-extensions-except={ext_path}",
                        f"--load-extension={ext_path}",
                        "--no-sandbox",
                    ],
                )
                browser = None
                ext_id = None
                if spec.get("extension_has_sw"):
                    sw_list = context.service_workers
                    if not sw_list:
                        sw_list = [context.wait_for_event("serviceworker", timeout=8000)]
                    ext_id = sw_list[0].url.split("/")[2]
            else:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(viewport={"width": 1280, "height": 720})
                ext_id = None

            def handle_route(route):
                url = route.request.url
                if _is_allowed_url(url):
                    route.continue_()
                else:
                    network_lines.append(f"BLOCKED {route.request.method} {url}")
                    route.abort()

            def handle_external_ws(ws):
                network_lines.append(f"BLOCKED WS {ws.url}")
                ws.close(code=1008, reason="Blocked external WebSocket in smoke E2E")

            context.route("**/*", handle_route)
            context.route_web_socket(
                re.compile(r"^wss?://(?!(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/|$))", re.IGNORECASE),
                handle_external_ws,
            )

            try:
                for page_spec in spec["pages"]:
                    rel_path = page_spec["path"]
                    if page_spec.get("is_extension_page"):
                        if not ext_id:
                            raise RuntimeError(f"{pkg}: extension ID could not be resolved")
                        target_url = f"chrome-extension://{ext_id}/{rel_path.lstrip('/')}"
                    elif spec.get("server"):
                        target_url = f"http://{SERVER_HOST}:{effective_port}/{rel_path.lstrip('/')}"
                    else:
                        target_url = (pkg_root / rel_path).resolve().as_uri()

                    page = context.new_page()

                    def on_console(msg, _url=target_url):
                        line = f"[{_url}] [{msg.type}] {msg.text}"
                        console_lines.append(line)
                        if msg.type == "error" and not _is_allowed_console_error(pkg, msg.text):
                            errors.append(f"console.error: {line}")

                    def on_pageerror(exc, _url=target_url):
                        line = f"[{_url}] {exc}"
                        pageerror_lines.append(line)
                        errors.append(f"pageerror: {line}")

                    def on_response(resp, _url=target_url):
                        if resp.status >= 400 and _is_allowed_url(resp.url) and not _is_allowed_http_error(pkg, resp.status, resp.url):
                            line = f"[{_url}] HTTP {resp.status} {resp.url}"
                            network_lines.append(line)
                            errors.append(f"http_error: {line}")

                    def on_requestfailed(req, _url=target_url):
                        if _is_allowed_url(req.url) and not _is_allowed_local_failure(pkg, req.url):
                            failure = req.failure or "unknown"
                            line = f"[{_url}] REQUEST_FAILED ({failure}) {req.url}"
                            network_lines.append(line)
                            errors.append(f"request_failed: {line}")

                    page.on("console", on_console)
                    page.on("pageerror", on_pageerror)
                    page.on("response", on_response)
                    page.on("requestfailed", on_requestfailed)

                    page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1200)

                    for sel in page_spec["selectors"]:
                        locator = page.locator(sel)
                        if locator.count() == 0:
                            errors.append(f"missing_selector: [{target_url}] selector '{sel}' not found")

                    try:
                        screenshots.append((rel_path, page.screenshot(full_page=True)))
                    except Exception:
                        pass

                    page.close()
            finally:
                context.close()
                if browser is not None:
                    browser.close()

        logs["console.log"] = "\n".join(console_lines)
        logs["pageerror.log"] = "\n".join(pageerror_lines)
        logs["network.log"] = "\n".join(network_lines)

    except Exception as e:
        errors.append(f"exception: {e}")
    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=2.0)
        if server_log_file is not None:
            server_log_file.close()
            server_log_path = extract_dir / "_e2e_server.log"
            if server_log_path.is_file():
                logs["server.log"] = server_log_path.read_text(encoding="utf-8", errors="replace")
        if obs_mock is not None:
            obs_mock.stop()
        if errors:
            _write_artifacts(artifacts_dir, pkg, logs, screenshots)
        if not keep_temp:
            tmp_ctx.cleanup()
        else:
            print(f"[{pkg}] kept temp dir: {extract_dir}")

    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(description="OBS_Widgets package smoke E2E (Phase 1)")
    parser.add_argument(
        "--package",
        action="append",
        choices=sorted(PACKAGE_SPECS.keys()),
        help="対象パッケージ名 (複数指定可、省略時は全10パッケージ)",
    )
    parser.add_argument(
        "--zip",
        help="既存 ZIP を指定してテスト (--package 1件と併用)",
    )
    parser.add_argument(
        "--version",
        default="0.0.0",
        help="一時ビルド時のバージョン文字列 X.Y.Z (既定: 0.0.0)",
    )
    parser.add_argument(
        "--artifacts-dir",
        help="失敗時のスクリーンショット・ログ保存先ディレクトリ",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="展開先一時ディレクトリを削除せず残す (デバッグ用)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="サーバー同梱パッケージの待受ポート上書き (OBS_WIDGETS_PORT)",
    )
    parser.add_argument(
        "--custom-port-check",
        type=int,
        default=18080,
        help="全パッケージ実行時に追加で行うカスタムポート起動確認ポート (0 でスキップ、既定: 18080)",
    )
    args = parser.parse_args()

    targets = args.package or list(PACKAGE_SPECS.keys())
    if args.zip and len(targets) != 1:
        print("error: --zip can only be used with a single --package", file=sys.stderr)
        return 2

    failed = []
    with tempfile.TemporaryDirectory(prefix="obs_e2e_zips_") as zip_out_dir:
        built_zips = {}
        for pkg in targets:
            if args.zip:
                zip_path = Path(args.zip).resolve()
            else:
                rc = build_package.main([pkg, args.version, "--out", zip_out_dir])
                if rc != 0:
                    print(f"[FAIL] {pkg} (build failed with code {rc})")
                    failed.append(pkg)
                    continue
                zip_path = Path(zip_out_dir) / f"{pkg}-v{args.version}.zip"
            built_zips[pkg] = zip_path
            ok, errs = run_package_smoke(
                pkg,
                zip_path,
                artifacts_dir=args.artifacts_dir,
                keep_temp=args.keep_temp,
                server_port=args.port,
            )
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] {pkg}")
            if not ok:
                failed.append(pkg)
                for err in errs:
                    print(f"  - {err}")

        if args.port is None and args.package is None and not args.zip and args.custom_port_check > 0:
            custom_pkg = "commentviewer"
            zip_path = built_zips.get(custom_pkg)
            if zip_path is not None:
                ok, errs = run_package_smoke(
                    custom_pkg,
                    zip_path,
                    artifacts_dir=args.artifacts_dir,
                    keep_temp=args.keep_temp,
                    server_port=args.custom_port_check,
                )
                status = "PASS" if ok else "FAIL"
                print(f"[{status}] {custom_pkg} (custom port {args.custom_port_check})")
                if not ok:
                    failed.append(f"{custom_pkg}@port{args.custom_port_check}")
                    for err in errs:
                        print(f"  - {err}")

    if failed:
        print(f"\nE2E smoke failed for: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nAll {len(targets)} package(s) passed E2E smoke.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
