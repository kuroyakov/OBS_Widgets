"""
最小 OBS WebSocket (v5) クライアント (server/obs_ws.py)

逐次リフレッシュの駆動をファイル I/O から直接実行へ移すための実装 (Issue #48)。
標準ライブラリ (socket) のみで WebSocket のハンドシェイク・フレーム送受信・
obs-websocket の認証 (Hello/Identify) とリクエスト (op 6/7) を行う。
新規の外部依存は入れない。

想定する使い方:
    with ObsWebSocketClient(password=..., timeout=5.0) as client:
        sources = client.list_browser_sources()   # [(inputName, url), ...]
        client.press_refresh(input_name)          # refreshnocache ボタンを押す
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

# obs-websocket v5 EventSubscription ビットマスク定義 (主要なもの)
EVENT_SUBSCRIPTION_NONE = 0
EVENT_SUBSCRIPTION_INPUTS = 1 << 3               # InputCreated / InputRemoved / InputNameChanged / InputMuteStateChanged 等
EVENT_SUBSCRIPTION_INPUT_VOLUME_METERS = 1 << 16  # 65536: 高頻度 InputVolumeMeters


class ObsWsError(Exception):
    def __init__(self, message, *, code=None, comment=""):
        super().__init__(message)
        self.code = code
        self.comment = comment


def _obs_auth_response(password, salt, challenge):
    """obs-websocket v5 の認証文字列を計算する"""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class ObsWebSocketClient:
    def __init__(self, host="127.0.0.1", port=4455, password="", timeout=5.0, event_subscriptions=0):
        self.host = host
        self.port = int(port)
        self.password = password or ""
        self.timeout = float(timeout)
        self.event_subscriptions = int(event_subscriptions)
        self._sock = None
        self._sock_lock = threading.Lock()
        self._buf = bytearray()
        self._frag_buf = bytearray()
        self._request_seq = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- 接続・ハンドシェイク -------------------------------------------------
    def connect(self):
        if self._sock is not None:
            return
        self._buf = bytearray()
        self._frag_buf = bytearray()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ObsWsError(f"OBS WebSocket へ接続できません: {e}") from e
        with self._sock_lock:
            self._sock = sock
        sock.settimeout(self.timeout)
        try:
            self._ws_handshake()
            hello = self._recv_message()
            if hello.get("op") != 0:
                raise ObsWsError(f"Hello を受信できませんでした: op={hello.get('op')}")
            identify = {"rpcVersion": 1, "eventSubscriptions": self.event_subscriptions}
            auth = (hello.get("d") or {}).get("authentication")
            if auth:
                identify["authentication"] = _obs_auth_response(
                    self.password, auth.get("salt", ""), auth.get("challenge", "")
                )
            self._send_message({"op": 1, "d": identify})
            identified = self._recv_message()
            if identified.get("op") != 2:
                raise ObsWsError(f"Identify が受理されませんでした: op={identified.get('op')}")
        except Exception:
            self.close()
            raise

    def _ws_handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: obswebsocket.json\r\n"
            "\r\n"
        )
        sock = self._sock
        if sock is None:
            raise ObsWsError("未接続です")
        sock.sendall(request.encode("ascii"))
        while True:
            sep_idx = self._buf.find(b"\r\n\r\n")
            if sep_idx != -1:
                header = bytes(self._buf[:sep_idx])
                del self._buf[:sep_idx + 4]
                break
            chunk = sock.recv(4096)
            if not chunk:
                raise ObsWsError("ハンドシェイク中に切断されました")
            self._buf.extend(chunk)
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status_line:
            raise ObsWsError(f"WebSocket ハンドシェイク失敗: {status_line}")

    # -- フレーム送受信 -------------------------------------------------------
    def _send_frame(self, first_byte, payload):
        sock = self._sock
        if sock is None:
            raise ObsWsError("未接続です")
        mask = os.urandom(4)
        header = bytearray([first_byte])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            sock.sendall(bytes(header) + masked)
        except OSError as e:
            raise ObsWsError(f"WebSocket 送信に失敗しました: {e}") from e

    def _send_message(self, obj):
        payload = json.dumps(obj).encode("utf-8")
        self._send_frame(0x81, payload)  # FIN + text

    def _try_pop_frame(self):
        """受信済みバッファに完全な1フレームが揃っていれば取り出して返す。不足なら None。"""
        buf = self._buf
        buf_len = len(buf)
        if buf_len < 2:
            return None
        first = buf[0]
        second = buf[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if buf_len < offset + 2:
                return None
            length = struct.unpack_from(">H", buf, offset)[0]
            offset += 2
        elif length == 127:
            if buf_len < offset + 8:
                return None
            length = struct.unpack_from(">Q", buf, offset)[0]
            offset += 8
        mask = None
        if masked:
            if buf_len < offset + 4:
                return None
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        if buf_len < offset + length:
            return None
        payload = bytes(buf[offset:offset + length])
        del buf[:offset + length]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _drain_buffered_json_message(self):
        """バッファ内に完全な JSON メッセージがあれば 1 件取り出す（Ping 応答・Close 検知を含む）。"""
        while True:
            frame = self._try_pop_frame()
            if frame is None:
                return None
            fin, opcode, chunk = frame
            if opcode == 0x8:  # close
                detail = ""
                if len(chunk) >= 2:
                    close_code = struct.unpack_from(">H", chunk, 0)[0]
                    reason = chunk[2:].decode("utf-8", "replace").strip()
                    detail = f" (code={close_code}: {reason})" if reason else f" (code={close_code})"
                raise ObsWsError(f"WebSocket が閉じられました{detail}")
            if opcode == 0x9:  # ping -> pong
                self._send_pong(chunk)
                continue
            if opcode in (0x1, 0x2, 0x0):  # text / binary / continuation
                self._frag_buf.extend(chunk)
                if fin:
                    raw_payload = bytes(self._frag_buf)
                    self._frag_buf.clear()
                    return json.loads(raw_payload.decode("utf-8"))
                continue
            # pong (0xA) 等は読み飛ばす

    def poll_message(self, timeout=0.5):
        """最大 timeout 秒待ち、1件の JSON メッセージを受信して返す。タイムアウト時は None。"""
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        sock = self._sock
        try:
            while True:
                msg = self._drain_buffered_json_message()
                if msg is not None:
                    return msg
                sock = self._sock
                if sock is None:
                    raise ObsWsError("WebSocket が切断されました")
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    sock.settimeout(remaining)
                else:
                    sock.settimeout(self.timeout)
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    return None
                except OSError as e:
                    raise ObsWsError(f"WebSocket 受信エラー: {e}") from e
                if not chunk:
                    raise ObsWsError("WebSocket が切断されました")
                self._buf.extend(chunk)
        finally:
            if sock is not None and self._sock is sock:
                try:
                    sock.settimeout(self.timeout)
                except OSError:
                    pass

    def _recv_message(self):
        """self.timeout 以内に 1 件の JSON メッセージを受信する（未着時は例外）"""
        msg = self.poll_message(timeout=self.timeout)
        if msg is None:
            raise ObsWsError("WebSocket 受信がタイムアウトしました")
        return msg

    def _send_pong(self, payload):
        # 長さのエンコーディングは _send_frame と同じ3段構えにする (Issue #87)。
        # 制御フレームは規格上 125 バイト以下だが、準拠形に揃える。
        self._send_frame(0x8A, payload)

    def close(self):
        with self._sock_lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    # -- obs-websocket リクエスト ----------------------------------------------
    def request(self, request_type, request_data=None):
        if self._sock is None:
            raise ObsWsError("未接続です")
        self._request_seq += 1
        request_id = f"muse-{self._request_seq}"
        self._send_message({
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": request_data or {},
            },
        })
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ObsWsError(f"{request_type} 応答がタイムアウトしました")
            message = self.poll_message(timeout=remaining)
            if message is None:
                raise ObsWsError(f"{request_type} 応答がタイムアウトしました")
            if message.get("op") != 7:
                continue
            data = message.get("d") or {}
            if data.get("requestId") != request_id:
                continue
            status = data.get("requestStatus") or {}
            if not status.get("result"):
                raise ObsWsError(
                    f"{request_type} が失敗しました: code={status.get('code')} {status.get('comment', '')}",
                    code=status.get("code"),
                    comment=status.get("comment", ""),
                )
            return data.get("responseData") or {}

    def list_browser_sources(self):
        """ブラウザソースの一覧を [(inputName, url), ...] で返す"""
        result = []
        data = self.request("GetInputList", {"inputKind": "browser_source"})
        for item in data.get("inputs", []):
            name = item.get("inputName")
            if not name:
                continue
            settings = self.request("GetInputSettings", {"inputName": name})
            url = (settings.get("inputSettings") or {}).get("url", "")
            result.append((name, url))
        return result

    def press_refresh(self, input_name):
        """ブラウザソースの「再読み込み (refreshnocache)」ボタンを押す"""
        self.request("PressInputPropertiesButton", {
            "inputName": input_name,
            "propertyName": "refreshnocache",
        })

    def get_input_list(self, input_kind=None):
        """入力ソース一覧を返す"""
        req = {"inputKind": input_kind} if input_kind else {}
        return self.request("GetInputList", req).get("inputs", [])

    def get_canvas_list(self):
        """キャンバス一覧を返す"""
        return self.request("GetCanvasList").get("canvases", [])

    def get_scene_list(self, canvas_uuid=None):
        """シーン一覧を返す"""
        req = {"canvasUuid": canvas_uuid} if canvas_uuid else {}
        return self.request("GetSceneList", req).get("scenes", [])

    def get_input_volume(self, input_name):
        """入力ソースの現在音量 (inputVolumeMul, inputVolumeDb) を返す"""
        return self.request("GetInputVolume", {"inputName": input_name})

    def set_input_volume(self, input_name, volume_db=None, volume_mul=None):
        """入力ソースの音量を設定する (dB または乗数)"""
        req = {"inputName": input_name}
        if volume_db is not None:
            req["inputVolumeDb"] = float(volume_db)
        elif volume_mul is not None:
            req["inputVolumeMul"] = float(volume_mul)
        else:
            raise ValueError("volume_db または volume_mul の指定が必要です")
        self.request("SetInputVolume", req)

    def get_input_mute(self, input_name):
        """入力ソースのミュート状態を bool で返す"""
        data = self.request("GetInputMute", {"inputName": input_name})
        return bool(data.get("inputMuted", False))

    def set_input_mute(self, input_name, muted):
        """入力ソースのミュート状態を設定する"""
        self.request("SetInputMute", {
            "inputName": input_name,
            "inputMuted": bool(muted),
        })

    def get_source_filter_list(self, source_name):
        """ソースに紐づくフィルタ一覧を返す"""
        data = self.request("GetSourceFilterList", {"sourceName": source_name})
        return data.get("filters", [])

    def set_source_filter_enabled(self, source_name, filter_name, enabled):
        """ソースのフィルタ有効/無効を切り替える"""
        self.request("SetSourceFilterEnabled", {
            "sourceName": source_name,
            "filterName": filter_name,
            "filterEnabled": bool(enabled),
        })

    def get_hotkey_list(self):
        """OBS ホットキー名一覧を返す"""
        return self.request("GetHotkeyList").get("hotkeys", [])

    def trigger_hotkey_by_name(self, hotkey_name, context_name=None):
        """OBS ホットキーを実行する"""
        req = {"hotkeyName": hotkey_name}
        if context_name:
            req["contextName"] = context_name
        self.request("TriggerHotkeyByName", req)

    def broadcast_custom_event(self, event_data):
        """CustomEvent を全クライアントへブロードキャストする"""
        self.request("BroadcastCustomEvent", {"eventData": event_data})

