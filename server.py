#!/usr/bin/env python3
"""
OBS Widgets 統合ローカルWebサーバー エントリポイント
実体ロジックは `server/` パッケージ配下の各モジュールに分割されています:
- server/config.py       : 設定・定数・twitch_config.json 管理
- server/twitch_api.py   : Twitch OAuth・Helix API・EventSub 登録
- server/translator.py   : 外国語→日本語翻訳・音声認識字幕ちゃん連動ステート
- server/voicevox.py     : ローカル VOICEVOX Engine 連携
- server/youtube_chat.py : YouTube Live チャット直接取得マネージャー
- server/obs_ws.py       : 最小 OBS WebSocket (v5) クライアント
- server/event_log.py    : サーバー常設イベントログ
- server/feed_log.py     : コメント・アラート履歴の記録 (UC-09)
- server/pngtuber_audio.py : PNGTuber 用 OBS 音声レベル計測・設定管理
- server/extensions.py   : 拡張ディレクトリ機構 (OBS_WIDGETS_EXTENSIONS_DIR / extensions/)
- server/handler.py      : HTTP リクエストハンドラ (UnifiedWidgetHandler)。
  拡張側 (extensions/server_modules/) の任意モジュール (launcher / browser_refresh /
  display_recapture / controller_status / avatar_layouts / cubase_bridge) および
  pngtuber_audio は任意で、不在の構成 (配布用) では関連 API が無効になる
"""

import errno
import os
import signal
import socket
import ssl
import subprocess
import time
import threading
from http.server import ThreadingHTTPServer

# OBS (obs_mothership.lua の os.execute) から子プロセスとして起動された際、
# OBS本体の開いているソケット (port 4455 等) のファイルディスクリプタを継承してしまい
# OBS再起動時やiPadからのWebSocket接続時にポート競合を起こすのを防ぐため、
# 標準入出力(0,1,2)以外の継承FDを起動直後にすべてクローズする。
try:
    os.closerange(3, 4096)
except Exception:
    pass

from server.config import PORT, BASE_DIR, TLS_PORT, TLS_CERT_FILE, TLS_KEY_FILE
from server.handler import UnifiedWidgetHandler, start_optional_modules

try:
    from server.pngtuber_audio import stop_pngtuber_audio as _stop_pngtuber_audio
except ImportError:
    _stop_pngtuber_audio = None

try:
    from server.cubase_bridge import stop_cubase_bridge as _stop_cubase_bridge
except ImportError:
    _stop_cubase_bridge = None


def _stop_optional_modules():
    if _stop_pngtuber_audio is not None:
        try:
            _stop_pngtuber_audio()
        except Exception:
            pass
    if _stop_cubase_bridge is not None:
        try:
            _stop_cubase_bridge()
        except Exception:
            pass


class ResilientThreadingHTTPServer(ThreadingHTTPServer):
    """IPv4 (0.0.0.0) のフォールバック用マルチスレッドHTTPサーバー"""
    allow_reuse_address = True
    daemon_threads = True

    # 同時処理数の上限 (Issue #95)。半開き接続などによるスレッド占有で
    # 正規のリクエストまで塞がれるのを防ぐ。上限超過の接続はキューに
    # 溜めず、その場で閉じて切り捨てる。
    MAX_CONCURRENT_REQUESTS = 32

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.Semaphore(self.MAX_CONCURRENT_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class DualStackThreadingHTTPServer(ResilientThreadingHTTPServer):
    """
    IPv6 (::) と IPv4 (0.0.0.0) の両方を単一ソケットで待ち受けるデュアルスタックサーバー (tcp46)。
    iPad Safari からの IPv4 直指定 (192.168.x.x)、IPv6 通信、および .local (Bonjour/mDNS) の
    すべてのアクセス方式に確実に応答する。
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()


class LoopbackV6ThreadingHTTPServer(ResilientThreadingHTTPServer):
    """IPv6 ループバック (::1) 専用のサーバー (Issue #117)。

    V6ONLY=1 で IPv4 マッピングを受け付けないため、127.0.0.1 側の
    リスナーと役割が重ならない純粋な ::1 専用になる。
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except Exception:
            pass
        super().server_bind()


def _create_server_on_port(port):
    """指定ポートでサーバーを生成する。

    DualStack (tcp46) を基本とし、IPv6 自体が使えない環境でのみ IPv4 (tcp4) へ
    フォールバックする。アドレス使用中の場合はフォールバックしない
    （先行プロセスが片方のアドレスファミリを掴んでいても、もう片方で
    双子サーバーを起動してしまうのを防ぐ）。使用中はしばらく待って
    リトライし、最終的にエラーとして呼び出し元へ返す。
    """
    last_err = None
    for attempt in range(5):
        # 1. Dual-Stack (IPv6 + IPv4 両対応) でバインド試行
        try:
            return DualStackThreadingHTTPServer(("::", port), UnifiedWidgetHandler)
        except OSError as e:
            last_err = e
            if e.errno == errno.EADDRINUSE:
                # 使用中はフォールバックせずリトライ（双子防止）
                if attempt < 4:
                    time.sleep(0.5)
                    continue
                break
            # 2. IPv6 自体が使えない環境の場合は IPv4 でバインド試行
            try:
                return ResilientThreadingHTTPServer(("0.0.0.0", port), UnifiedWidgetHandler)
            except OSError as e2:
                last_err = e2
                if e2.errno == errno.EADDRINUSE and attempt < 4:
                    time.sleep(0.5)
                    continue
                break
    raise last_err


def _create_loopback_server(host, port):
    """ループバックアドレス (127.0.0.1 / ::1) 専用のサーバーを生成する (Issue #117)。

    使用中の場合は双子サーバー防止のためフォールバックせずリトライし、
    最終的にエラーとして呼び出し元へ返す (_create_server_on_port と同じ方針)。
    """
    server_class = (
        LoopbackV6ThreadingHTTPServer if ":" in host else ResilientThreadingHTTPServer
    )
    last_err = None
    for attempt in range(5):
        try:
            return server_class((host, port), UnifiedWidgetHandler)
        except OSError as e:
            last_err = e
            if e.errno == errno.EADDRINUSE and attempt < 4:
                time.sleep(0.5)
                continue
            raise
    raise last_err


def _start_http_loopback_servers():
    """HTTP リスナーをループバック限定で起動する (Issue #94 フェーズ4 / #117)。

    平文 HTTP は Mac の中だけに閉じ、LAN を渡る通信は TLS (8443) に寄せる。
    127.0.0.1 と ::1 の2本立てとし、片方が使えない環境 (IPv6 無効など) でも
    残った方で継続する。両方とも生成できなければ例外として起動を止める。
    """
    servers = []
    errors = []
    for host in ("127.0.0.1", "::1"):
        try:
            servers.append(_create_loopback_server(host, PORT))
        except Exception as e:
            errors.append((host, e))
    if not servers:
        detail = "; ".join(f"{host}: {err}" for host, err in errors)
        raise RuntimeError(f"HTTP loopback listeners failed: {detail}")
    for host, err in errors:
        print(f"[HTTP] loopback listener on {host}:{PORT} failed: {err}", flush=True)
    for srv in servers[1:]:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
    for srv in servers:
        host = srv.server_address[0]
        print(f"Unified Server running on {host} port {PORT} (loopback only)", flush=True)
    return servers


def _start_tls_server_if_configured():
    """TLS リスナーを追加起動する (Issue #94 フェーズ1 / #113)。

    証明書と秘密鍵 (certs/tls-cert.pem, certs/tls-key.pem) の両方が置かれて
    いる場合のみ、HTTPS リスナーを別スレッドで起動して返す。ファイルが
    無ければ何もせず None を返すため、証明書が無い環境では従来通りの
    HTTP のみの動作になる。読み込みやバインドに失敗した場合も、移行期間中
    の可用性を優先してエラーを表示した上で HTTP のみで継続する
    (HTTP を閉じるかどうかは #94 フェーズ4で判断する)。
    """
    if not (os.path.isfile(TLS_CERT_FILE) and os.path.isfile(TLS_KEY_FILE)):
        return None
    httpd_tls = None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
        httpd_tls = _create_server_on_port(TLS_PORT)
        httpd_tls.socket = context.wrap_socket(httpd_tls.socket, server_side=True)
        # OBS WebSocket トンネル (Issue #115) は TLS リスナーでのみ有効にする
        httpd_tls.obs_ws_tunnel_enabled = True
    except Exception as e:
        print(f"[TLS] HTTPS listener on port {TLS_PORT} failed to start: {e}", flush=True)
        if httpd_tls is not None:
            try:
                httpd_tls.server_close()
            except Exception:
                pass
        return None
    thread = threading.Thread(target=httpd_tls.serve_forever, daemon=True)
    thread.start()
    print(f"Unified Server (HTTPS) running on port {TLS_PORT}", flush=True)
    return httpd_tls


def _process_name(pid):
    """ps でプロセス名を引く。引けない場合は空文字（判定をスキップする）"""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _owner_watchdog(httpd, owner_pid):
    """オーナー（OBS）の生存を監視し、消滅したらサーバーを終了させる。

    OBS の正常終了・クラッシュ・強制終了のいずれでも、終了イベント経由の
    停止処理に頼らずサーバーが残留しないようにするための仕組み。
    PID の再利用に備え、プロセス名の変化でも消滅とみなす。
    """
    owner_name = ""
    tick = 0
    while True:
        time.sleep(2)
        tick += 1
        try:
            os.kill(owner_pid, 0)
        except OSError:
            break
        if tick % 5 == 1:
            name = _process_name(owner_pid)
            if name:
                if not owner_name:
                    owner_name = name
                elif name != owner_name:
                    break
    print(f"Owner process {owner_pid} is gone. Stopping Unified Server...", flush=True)
    _stop_optional_modules()
    try:
        httpd.shutdown()
    except Exception:
        pass
    os._exit(0)


def run():
    os.chdir(BASE_DIR)

    # メインサーバー (port 8080)。互換用サブリスナー (旧 port 8000) は Issue #76 で廃止。
    # Issue #117 でループバック限定化し、LAN 向けは TLS (8443) のみになった
    http_servers = _start_http_loopback_servers()
    httpd = http_servers[0]

    # TLS (Issue #94 フェーズ1)。証明書が置かれている場合のみ HTTPS を併用する
    httpd_tls = _start_tls_server_if_configured()

    # 読み込み済みの任意常駐モジュールを起動する (Issue #37)
    start_optional_modules()

    # OBS (obs_mothership.lua) から起動された場合は、OBS の PID が
    # OBS_WIDGETS_OWNER_PID で渡される。オーナーの消滅を監視して自動終了する。
    # 手動起動 (start_server.command) では未設定のため監視しない。
    owner_pid_raw = os.environ.get("OBS_WIDGETS_OWNER_PID", "").strip()
    if owner_pid_raw.isdigit() and int(owner_pid_raw) > 0:
        owner_pid = int(owner_pid_raw)
        watchdog = threading.Thread(
            target=_owner_watchdog, args=(httpd, owner_pid), daemon=True
        )
        watchdog.start()
        print(f"Watching owner process PID {owner_pid} (server stops when it exits)", flush=True)

    def _handle_sigterm(_signum, _frame):
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _handle_sigterm)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nStopping Unified Server...")
        _stop_optional_modules()
        if httpd_tls is not None:
            httpd_tls.shutdown()
            httpd_tls.server_close()
        for srv in http_servers:
            try:
                srv.shutdown()
            except Exception:
                pass
            srv.server_close()


if __name__ == "__main__":
    run()
