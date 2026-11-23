"""
HTTP リクエストハンドラモジュール (server/handler.py)
- 静的ファイル配信 (SimpleHTTPRequestHandler 継承) ＆ CORS / キャッシュ無効化ヘッダ付与
- GET / POST API エンドポイントのルーティング
"""

import html
import hmac
import ipaddress
import json
import os
import posixpath
import re
import secrets
import sys
import select
import socket
import ssl
import threading
import time
import urllib.request
from datetime import datetime
import urllib.parse
from http.server import SimpleHTTPRequestHandler

from .config import (
    PORT,
    TLS_PORT,
    BASE_DIR,
    SSL_CONTEXT,
    REQUIRED_SCOPES,
    CATEGORY_PRESETS,
    DEFAULT_COMMENT_VIEWER_SETTINGS,
    OBS_WS_TUNNEL_PATH,
    OBS_WS_UPSTREAM_HOST,
    OBS_WS_UPSTREAM_PORT,
    load_twitch_config,
    save_twitch_config,
    get_or_create_api_token
)
from .twitch_api import (
    validate_twitch_token,
    twitch_request,
    get_broadcaster_id_by_login,
    register_eventsub_subscriptions
)
from .translator import (
    get_current_subtitle_lang_status,
    update_subtitle_lang_state,
    translate_to_japanese
)
from .voicevox import (
    fetch_voicevox_speakers,
    synthesize_voicevox_wav
)
from .youtube_chat import YOUTUBE_MANAGER
from .event_log import log_event
from .feed_log import record_events as feed_log_record_events
from .feed_log import session_open as feed_log_session_open
from .extensions import (
    discover_static_mounts,
    get_extension_module_path,
    import_symbols,
    load_optional_module,
    resolve_extensions_dir,
    translate_extension_path,
)

# --- 拡張ディレクトリ機構 (Issue #220 / #222) ---
# 環境変数 OBS_WIDGETS_EXTENSIONS_DIR (絶対パス) が指定されていればそれを優先し、
# 未設定時は BASE_DIR/extensions/ が存在すればデフォルトとして使用する（空文字は明示的な無効化）。
# 公開側ツリー外の静的ディレクトリと任意モジュール (server_modules/<name>.py) を読み込む。
_EXTENSIONS_DIR = resolve_extensions_dir(base_dir=BASE_DIR)
_EXTENSION_STATIC_MOUNTS = discover_static_mounts(BASE_DIR, _EXTENSIONS_DIR)

# --- Controller 系モジュールの任意 import (Issue #150 PR-3 / Issue #220) ---
# 配布パッケージ (CommentViewer セット) には Controller 系の4モジュール
# (launcher / browser_refresh / display_recapture / controller_status) を
# 含めないため、import の成否がそのまま有効/無効の信号になる。
# 環境変数 OBS_WIDGETS_SERVER_PROFILE=commentviewer を指定すると、ファイルが
# 存在する開発環境でも無効状態を再現できる。
# 無効時の各エンドポイントの振る舞いは設計 (Issue #150) どおり:
# - POST /api/launcher/action の page_ready だけは 200 で受け流し、他は 404
# - GET /api/launcher/status・/api/heartbeat、POST /api/twitch/set-category・
#   set-title は 404
# - GET /api/config の LAN 情報は空で返し、字幕言語取得時のハートビート記録は
#   何もしない
# - ルートのリダイレクト先は /Controller/ でなく /CommentViewer/ になる
_SERVER_PROFILE = os.environ.get("OBS_WIDGETS_SERVER_PROFILE", "").strip().lower()
_CONTROLLER_FORCED_OFF = _SERVER_PROFILE == "commentviewer"


def _warn_controller_import_error(module_name, exc, *, check_extension=True):
    """Controller 系モジュールの import 失敗時の警告 (Issue #150 PR-3 / Issue #220)。

    配布構成でファイル自体が無いのは正常な無効化だが、BASE_DIR/server/ または
    拡張側 server_modules/ にファイルが存在するのに import に失敗した場合は
    開発環境側の不具合（import 名の誤り等）の可能性が高いため、無言で無効化せず
    標準エラーへ警告を出す（サーバー自体は継続する）。
    """
    if _CONTROLLER_FORCED_OFF:
        return
    if os.path.isfile(os.path.join(BASE_DIR, "server", f"{module_name}.py")):
        print(f"[handler] 警告: server/{module_name}.py は存在しますが import に失敗しました"
              f" (Controller 系機能は無効化されます): {exc}", file=sys.stderr)
        return
    if check_extension:
        ext_path = get_extension_module_path(module_name, _EXTENSIONS_DIR)
        if ext_path:
            print(f"[handler] 警告: {ext_path} は存在しますが import に失敗しました"
                  f" (Controller 系機能は無効化されます): {exc}", file=sys.stderr)


# launcher が browser_refresh / display_recapture を参照するため、依存順に読み込む。
try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _browser_refresh_mod = load_optional_module(
        "browser_refresh", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (_note_page_ready,) = import_symbols(_browser_refresh_mod, ("note_page_ready",))
except ImportError as e:
    _warn_controller_import_error("browser_refresh", e)
    _note_page_ready = None

try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    load_optional_module(
        "display_recapture", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
except ImportError as e:
    _warn_controller_import_error("display_recapture", e)

_loading_controller_module = "launcher"
try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _launcher_mod = load_optional_module(
        "launcher", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (execute_launcher_action,) = import_symbols(_launcher_mod, ("execute_launcher_action",))
    _loading_controller_module = "controller_status"
    _controller_status_mod = load_optional_module(
        "controller_status", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (
        record_heartbeat,
        get_lan_network_info,
        get_all_launcher_status,
    ) = import_symbols(
        _controller_status_mod,
        ("record_heartbeat", "get_lan_network_info", "get_all_launcher_status"),
    )
    _CONTROLLER_ENABLED = True
except ImportError as e:
    _warn_controller_import_error(_loading_controller_module, e)
    execute_launcher_action = None
    record_heartbeat = None
    get_lan_network_info = None
    get_all_launcher_status = None
    _CONTROLLER_ENABLED = False

try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _avatar_layouts_mod = load_optional_module(
        "avatar_layouts", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (
        _load_avatar_layouts,
        _save_avatar_layouts,
    ) = import_symbols(_avatar_layouts_mod, ("load_avatar_layouts", "save_avatar_layouts"))
    _AVATAR_LAYOUTS_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("avatar_layouts", e)
    _load_avatar_layouts = None
    _save_avatar_layouts = None
    _AVATAR_LAYOUTS_ENABLED = False

# pngtuber_audio は公開側モジュールのため拡張ディレクトリ対象外 (Issue #220)
try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    from .pngtuber_audio import (
        IMAGE_SLOTS as _PNGTUBER_IMAGE_SLOTS,
        MAX_PNGTUBER_IMAGE_BYTES as _MAX_PNGTUBER_IMAGE_BYTES,
        get_pngtuber_level as _get_pngtuber_level,
        get_pngtuber_client_config as _get_pngtuber_client_config,
        resolve_pngtuber_image_file as _resolve_pngtuber_image_file,
        register_pngtuber_stream as _register_pngtuber_stream,
        unregister_pngtuber_stream as _unregister_pngtuber_stream,
        wait_pngtuber_stream_update as _wait_pngtuber_stream_update,
        is_pngtuber_stopped as _is_pngtuber_stopped,
        save_pngtuber_config as _save_pngtuber_config,
        save_pngtuber_image_upload as _save_pngtuber_image_upload,
        list_pngtuber_images as _list_pngtuber_images,
        list_obs_audio_inputs as _list_obs_audio_inputs,
    )
    _PNGTUBER_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("pngtuber_audio", e, check_extension=False)
    _PNGTUBER_IMAGE_SLOTS = ()
    _MAX_PNGTUBER_IMAGE_BYTES = 10 * 1024 * 1024
    _get_pngtuber_level = None
    _get_pngtuber_client_config = None
    _resolve_pngtuber_image_file = None
    _register_pngtuber_stream = None
    _unregister_pngtuber_stream = None
    _wait_pngtuber_stream_update = None
    _is_pngtuber_stopped = None
    _save_pngtuber_config = None
    _save_pngtuber_image_upload = None
    _list_pngtuber_images = None
    _list_obs_audio_inputs = None
    _PNGTUBER_ENABLED = False

try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _pngtuber_mic_assignment_mod = load_optional_module(
        "pngtuber_mic_assignment", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (
        _get_pngtuber_mic_assignment,
        _update_pngtuber_mic_assignment,
    ) = import_symbols(
        _pngtuber_mic_assignment_mod,
        ("get_pngtuber_mic_assignment", "update_pngtuber_mic_assignment"),
    )
    _PNGTUBER_MIC_ASSIGNMENT_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("pngtuber_mic_assignment", e)
    _get_pngtuber_mic_assignment = None
    _update_pngtuber_mic_assignment = None
    _PNGTUBER_MIC_ASSIGNMENT_ENABLED = False

try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _cubase_bridge_mod = load_optional_module(
        "cubase_bridge", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (
        _ingest_cubase_event,
        _get_cubase_state,
        _register_cubase_stream,
        _unregister_cubase_stream,
        _wait_cubase_stream_update,
        _is_cubase_stopped,
    ) = import_symbols(
        _cubase_bridge_mod,
        (
            "ingest_event",
            "get_state",
            "register_stream",
            "unregister_stream",
            "wait_stream_update",
            "is_stopped",
        ),
    )
    _clear_cubase_display = getattr(_cubase_bridge_mod, "clear_display", None)
    if not callable(_clear_cubase_display):
        _clear_cubase_display = None
    _CUBASE_BRIDGE_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("cubase_bridge", e)
    _ingest_cubase_event = None
    _get_cubase_state = None
    _register_cubase_stream = None
    _unregister_cubase_stream = None
    _wait_cubase_stream_update = None
    _is_cubase_stopped = None
    _clear_cubase_display = None
    _CUBASE_BRIDGE_ENABLED = False

try:
    _voice_command_mod = load_optional_module(
        "voice_command", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (
        _load_voice_command_config,
        _save_voice_command_config,
        _get_voice_command_obs_resources,
        _get_voice_command_history,
        _process_voice_command_utterance,
    ) = import_symbols(
        _voice_command_mod,
        (
            "load_voice_command_config",
            "save_voice_command_config",
            "get_obs_resources",
            "get_history",
            "process_utterance",
        ),
    )
    _VOICE_COMMAND_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("voice_command", e, check_extension=False)
    _load_voice_command_config = None
    _save_voice_command_config = None
    _get_voice_command_obs_resources = None
    _get_voice_command_history = None
    _process_voice_command_utterance = None
    _VOICE_COMMAND_ENABLED = False

try:
    if _CONTROLLER_FORCED_OFF:
        raise ImportError("controller modules disabled by OBS_WIDGETS_SERVER_PROFILE")
    _subtitle_mute_link_mod = load_optional_module(
        "subtitle_mute_link", base_dir=BASE_DIR, extensions_dir=_EXTENSIONS_DIR
    )
    (_start_subtitle_mute_link,) = import_symbols(
        _subtitle_mute_link_mod, ("start_subtitle_mute_link",)
    )
    if not callable(_start_subtitle_mute_link):
        raise ImportError("start_subtitle_mute_link is not callable")
    _SUBTITLE_MUTE_LINK_ENABLED = True
except ImportError as e:
    _warn_controller_import_error("subtitle_mute_link", e)
    _start_subtitle_mute_link = None
    _SUBTITLE_MUTE_LINK_ENABLED = False


def start_optional_modules():
    """読み込みに成功した常駐型の任意モジュールを起動する (Issue #37)。

    起動に失敗した場合も標準エラーへ警告を出すのみで、サーバー全体の起動を妨げない。
    """
    if _SUBTITLE_MUTE_LINK_ENABLED and callable(_start_subtitle_mute_link):
        try:
            _start_subtitle_mute_link()
        except Exception as e:
            print(
                f"[handler] 警告: subtitle_mute_link の起動に失敗しました"
                f" (サーバー起動は継続します): {e}",
                file=sys.stderr,
            )


def _settings_guide():
    """設定変更の入口の案内文 (OAuth エラー画面用)。

    フル構成では Controller の設定モーダルが入口だが、CommentViewer
    パッケージ構成 (Controller 系モジュール無効時) には Controller が
    存在しないため、設定ファイル twitch_config.json を案内する。
    """
    if _CONTROLLER_ENABLED:
        return "Controller の「⚙️ Twitch設定」"
    return "twitch_config.json"


def _oauth_retry_guide_prefix():
    """認可のやり直し方法の案内文の前半 (OAuth エラー画面用)。

    後半は呼び出し側が持つ: state 不一致は「認可を最初からやり直してください」、
    チャンネル不一致は「認可をやり直してください」(従来文言をそのまま維持する)。
    """
    if _CONTROLLER_ENABLED:
        return "Controller の「⚙️ Twitch設定」から"
    return "CommentViewer の管理パネルにある認可ボタンから"


# --- Twitch OAuth の state 管理 (Issue #92) ---
# 認可開始時に推測困難な state を発行し、コールバックで一度だけ照合することで、
# 他者の認可コード／トークンを保存させられるログイン CSRF を防ぐ。
# 保持はサーバーのメモリ上のみ（再起動で消える。その場合は認可をやり直せばよい）。
_OAUTH_STATE_TTL = 600  # 秒
_pending_oauth_state = {"value": None, "expires": 0.0}


def _issue_oauth_state():
    value = secrets.token_urlsafe(32)
    _pending_oauth_state["value"] = value
    _pending_oauth_state["expires"] = time.time() + _OAUTH_STATE_TTL
    return value


def _consume_oauth_state(provided):
    """state を照合し、成否にかかわらず破棄する（一度だけ有効）。"""
    expected = _pending_oauth_state["value"]
    expires = _pending_oauth_state["expires"]
    _pending_oauth_state["value"] = None
    _pending_oauth_state["expires"] = 0.0
    if not expected or not provided:
        return False
    if time.time() > expires:
        return False
    return hmac.compare_digest(str(provided), expected)


def _oauth_redirect_uri(host_header, server_port):
    """OAuth の戻り先 URL を、フローを開始したリクエストのホストから組み立てる (Issue #136)。

    Mac の localhost:8080 と iPad の .local:8443 のどちらからでも認可を
    完了できるようにするためのもの。Host は localhost / *.local /
    プライベート IP のみ許可し、それ以外 (外部ホストの差し込み) は
    localhost にフォールバックする。スキームはリクエストを受けた
    リスナーのポートで決める (TLS_PORT = HTTPS)。コールバック側でも
    同じ関数で再構成し、認可開始時とトークン交換時で一致させる。
    """
    host = (host_header or "").strip()
    if host.startswith("["):
        hostname = host[1:].split("]", 1)[0].lower()
    elif ":" in host:
        hostname = host.rsplit(":", 1)[0].lower()
    else:
        hostname = host.lower()
    allowed = hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".local")
    if not allowed and hostname:
        try:
            allowed = ipaddress.ip_address(hostname).is_private
        except ValueError:
            allowed = False
    if not host or not allowed:
        return f"http://localhost:{PORT}/api/twitch/callback"
    scheme = "https" if server_port == TLS_PORT else "http"
    # ホスト名はブラウザが小文字化して送ってくる (URL の仕様でホスト部は
    # 大文字小文字を区別しない) が、Twitch の登録照合は文字列の完全一致の
    # ため、生成側でも小文字に正規化して登録文字列と揃える (Issue #136)。
    return f"{scheme}://{host.lower()}/api/twitch/callback"


# --- トークン保存時のチャンネル一致チェック (Issue #99) ---
# state (Issue #92) は「開始した認可要求への応答か」までしか保証しないため、
# 認可ループを自分で完結させた別アカウントのトークンの保存は止まらない。
# ここでは保存直前にトークンを検証し、broadcaster_id が設定済みで、検証できた
# トークンのユーザーがそれと異なる場合のみ保存を拒否する。
# ・broadcaster_id 未設定（初回設定）なら常に通す
# ・トークン自体が無効な場合は対象外とし、従来の挙動を維持する
# ・検証エンドポイントへ到達できない（ネットワークエラー）場合は
#   ("unreachable") を返し、呼び出し側はフェイルオープン（保存を通し、記録する）
# 検証には対象トークンだけを渡す。保存済みの refresh_token を一緒に渡すと、
# validate 側の 401 自動リフレッシュが旧トークンを更新してしまう副作用があるため。
def _check_token_channel_match(access_token, configured_broadcaster_id):
    """(verdict, token_user_id) を返す。verdict は None / "mismatch" / "unreachable"。"""
    if not configured_broadcaster_id or not access_token:
        return (None, "")
    info = validate_twitch_token({"access_token": access_token})
    if info.get("valid"):
        user_id = str(info.get("user_id") or "")
        if user_id and user_id != str(configured_broadcaster_id):
            return ("mismatch", user_id)
        return (None, user_id)
    if "error" in info:
        return ("unreachable", "")
    return (None, "")


# --- /api/translate のレート制限 (Issue #95) ---
# IP あたり 60回/分の固定ウィンドウ。/api/translate は無認証で外部の翻訳
# エンドポイントを叩くため、LAN 内からの増幅を防ぐ上限として置く。
# 超過分は 429 で返し、クライアント側で翻訳なし表示に劣化させる。
# 正規利用（外国語コメントの翻訳）は毎分数件〜十数件の規模で、当たることは想定しない。
_TRANSLATE_RATE_LIMIT = 60
_translate_rate_lock = threading.Lock()
_translate_rate_windows = {}  # ip -> [ウィンドウ番号, 回数]


def _check_translate_rate(ip):
    window = int(time.time() // 60)
    with _translate_rate_lock:
        entry = _translate_rate_windows.get(ip)
        if entry is None or entry[0] != window:
            _translate_rate_windows[ip] = [window, 1]
            if len(_translate_rate_windows) > 512:
                for stale in [k for k, v in _translate_rate_windows.items() if v[0] != window]:
                    del _translate_rate_windows[stale]
            return True
        if entry[1] >= _TRANSLATE_RATE_LIMIT:
            return False
        entry[1] += 1
        return True


def _parse_multipart_file_upload(content_type, body_bytes):
    """multipart/form-data ボディからアップロードされた (filename, file_bytes) を抽出する。"""
    boundary = None
    for part in content_type.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part[len("boundary="):].strip()
            if len(boundary) >= 2 and boundary[0] == '"' and boundary[-1] == '"':
                boundary = boundary[1:-1]
            break
    if not boundary:
        raise ValueError("Missing multipart boundary")

    delimiter = b"--" + boundary.encode("latin-1", "ignore")
    chunks = body_bytes.split(delimiter)
    selected = None
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        sep = b"\r\n\r\n"
        sep_idx = chunk.find(sep)
        if sep_idx == -1:
            sep = b"\n\n"
            sep_idx = chunk.find(sep)
            if sep_idx == -1:
                continue
        header_bytes = chunk[:sep_idx]
        payload = chunk[sep_idx + len(sep):]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]

        headers_text = header_bytes.decode("utf-8", "replace")
        disp = ""
        for line in headers_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "content-disposition":
                    disp = v.strip()
                    break
        if not disp:
            continue
        field_name = ""
        filename = None
        m_name = re.search(r';\s*name="([^"]*)"', disp)
        if m_name:
            field_name = m_name.group(1)
        m_fn_star = re.search(r";\s*filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\s]+)", disp)
        if m_fn_star:
            filename = urllib.parse.unquote(m_fn_star.group(1))
        else:
            m_fn = re.search(r';\s*filename="((?:\\.|[^"\\])*)"', disp)
            if m_fn:
                filename = m_fn.group(1).replace('\\"', '"').replace("\\\\", "\\")
            else:
                m_fn_bare = re.search(r";\s*filename=([^;\s]+)", disp)
                if m_fn_bare:
                    filename = m_fn_bare.group(1)
        if filename is not None:
            if field_name == "file":
                return filename, payload
            if selected is None:
                selected = (filename, payload)
    if selected is not None:
        return selected
    raise ValueError("No file field found in multipart upload")


class UnifiedWidgetHandler(SimpleHTTPRequestHandler):
    # POST ボディの上限 (Issue #86)。正規の API は小さな JSON のみで、1MiB は十分な余裕
    MAX_POST_BODY = 1024 * 1024
    # ボディ読み込みのタイムアウト秒数 (Issue #86)。半開き接続によるスレッド占有を防ぐ
    POST_READ_TIMEOUT = 15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def translate_path(self, path):
        ext_path = translate_extension_path(path, _EXTENSION_STATIC_MOUNTS)
        if ext_path is not None:
            return ext_path
        return super().translate_path(path)

    def log_message(self, format, *args):
        # 定期ポーリングやブラウザ自動リクエスト(favicon等)によるターミナルログ出力を抑制
        if len(args) >= 2 and isinstance(args[0], str):
            req_line = args[0]
            status_str = str(args[1])
            if status_str in ("200", "204"):
                if (
                    "GET /api/subtitle/lang" in req_line
                    or "GET /api/youtube/poll" in req_line
                    or "GET /api/launcher/status" in req_line
                    or "GET /api/config" in req_line
                    or "GET /api/pngtuber/level" in req_line
                    or "GET /api/pngtuber/stream" in req_line
                    or "favicon.ico" in req_line
                    or "apple-touch-icon" in req_line
                    or "onesdk.js" in req_line
                    or ".well-known" in req_line
                ):
                    return
        super().log_message(format, *args)

    @staticmethod
    def _origin_is_allowed(origin):
        """CORS を許可する Origin かをホスト名単位で厳密に判定する (Issue #64)。

        前方一致・部分一致は使わない: `localhost.evil.example` や
        `evil.local.evil.com`、プライベート帯域外の 172.x 等は許可しない。
        """
        if not origin or origin == "null":
            # Origin なし (同一オリジン等) と file:// 由来の "null" は従来どおり許可
            return True
        # chrome-extension:// は一律許可しない (Issue #96)。自前拡張は拡張オリジンで
        # 通信しない (jimakuChan-auto-lang は字幕ちゃんページ内で動作し Origin は
        # sayonari.github.io / www.sayonari.com、MixConsole はサーバーへアクセスしない)
        # ため正規利用に不要。
        try:
            parsed = urllib.parse.urlparse(origin)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        # 字幕ちゃん公式ページは v2 (sayonari.github.io) と v1 (www.sayonari.com) の
        # 両方を許可する。jimakuChan-auto-lang の manifest が対象に含むドメインと
        # 揃える (Issue #185 [7-3])。
        if host == "localhost" or host.endswith(".local") or host == "sayonari.github.io" or host == "www.sayonari.com":
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def end_headers(self):
        origin = (self.headers.get("Origin") or "").strip()
        # ローカルホスト・同一LAN(IPv4/IPv6/.local)・字幕ちゃん(sayonari.github.io / www.sayonari.com)・file:// から CORS を許可
        if self._origin_is_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin if origin else "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Widget-Token")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def send_json(self, data, status_code=200):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_html(self, html_text, status_code=200):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_text.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- API トークン認証 (Issue #46: 案C＋3) ---
    def _client_ip(self):
        ip = self.client_address[0] or ""
        # デュアルスタック受けでは IPv4 が ::ffff:a.b.c.d と表れるため正規化する
        if ip.startswith("::ffff:"):
            ip = ip[len("::ffff:"):]
        return ip

    def _client_is_local(self):
        ip = self._client_ip()
        return ip == "::1" or ip.startswith("127.")

    def _has_valid_api_token(self):
        expected = get_or_create_api_token()
        provided = self.headers.get("X-Widget-Token", "")
        return bool(expected) and hmac.compare_digest(provided, expected)

    def _can_write(self):
        """状態変更を許可するか: localhost 発信か、有効なトークンを所持している場合のみ。"""
        return self._client_is_local() or self._has_valid_api_token()

    def _visible_obs_password(self, config):
        """OBS パスワードは localhost / トークン所持者にのみ返す (それ以外は空文字)。"""
        return config.get("obs_password", "") if self._can_write() else ""

    def _handle_obs_ws_tunnel(self):
        """wss://<host>:8443/obs-ws を OBS WebSocket (ws://127.0.0.1:4455) へ中継する (Issue #115)。

        HTTPS ページから平文 ws:// への接続は混合コンテンツとしてブラウザに
        遮断されるため、TLS の終端をこのサーバーが担い、WebSocket の
        ハンドシェイクとフレームは一切解釈せずバイト列のまま双方向に流す。
        OBS のパスワード認証もエンドツーエンドでそのまま効くので、
        サーバーは資格情報を扱わない。中継中はこのハンドラのスレッドと
        同時接続上限 (Issue #95) の枠を1つ占有し続ける。
        """
        upgrade = (self.headers.get("Upgrade") or "").lower()
        connection_hdr = (self.headers.get("Connection") or "").lower()
        if "websocket" not in upgrade or "upgrade" not in connection_hdr:
            self.send_json({"error": "WebSocket upgrade required"}, 400)
            return
        try:
            upstream = socket.create_connection(
                (OBS_WS_UPSTREAM_HOST, OBS_WS_UPSTREAM_PORT), timeout=10
            )
        except OSError:
            self.send_json({"error": "OBS WebSocket unreachable"}, 502)
            return
        # この接続は HTTP としてはここで終わり、以降は素通しの中継になる
        self.close_connection = True
        client = self.connection
        peer = self.client_address[0] if self.client_address else "?"
        try:
            head = [self.requestline]
            head.extend(f"{key}: {value}" for key, value in self.headers.items())
            upstream.sendall(
                ("\r\n".join(head) + "\r\n\r\n").encode("iso-8859-1", "replace")
            )
            print(f"[obs-ws] tunnel open from {peer}", flush=True)
            sockets = [client, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 30)
                # TLS の復号バッファ内に残っている分は select に見えないため補う
                if (
                    isinstance(client, ssl.SSLSocket)
                    and client.pending()
                    and client not in readable
                ):
                    readable = [*readable, client]
                closed = False
                for src in readable:
                    data = src.recv(65536)
                    if not data:
                        closed = True
                        break
                    (upstream if src is client else client).sendall(data)
                if closed:
                    break
        except OSError:
            pass
        finally:
            print(f"[obs-ws] tunnel close from {peer}", flush=True)
            try:
                upstream.close()
            except OSError:
                pass

    def _is_static_blocked(self, unquoted_path):
        # 静的ファイルガードの判定 (do_GET の 0-0 / 0-0B と do_HEAD で共用)。
        #   (Issue #173 で certs/・logs/・tools/ ディレクトリ、avatar_layouts.json、.pem/.key/.jsonl 拡張子を追加)
        #   (Issue #185 [6-2] で破損 config 退避ファイル twitch_config.json.corrupt* を追加。
        #    退避ファイルには client_secret 等が平文で含まれるため、twitch_config.json と同じ扱いで遮断する)
        #   隠し判定はファイル名だけでなくパス区画全体で行う（/.git/ や /.gh/ 配下の素通りを防ぐ。.well-known は 0-A の 204 応答のため例外）
        #   先頭区画の判定は /%2fserver/ や /a/../logs/ のような迂回にも効くよう正規化後の区画単位で行う
        normalized_path = posixpath.normpath("/" + unquoted_path.replace("\\", "/").lstrip("/"))
        lower_path = normalized_path.lower()
        basename = lower_path.rsplit("/", 1)[-1]
        path_segments = [seg for seg in lower_path.split("/") if seg]
        if (
            basename in ("twitch_config.json", "launcher_config.json", "avatar_layouts.json", "pngtuber_config.json", "cubase_bridge_config.json", "voice_command_config.json")
            or basename.startswith("twitch_config.json.")
            or basename.startswith("pngtuber_config.json.")
            or basename.startswith("voice_command_config.json.")
            or (path_segments and path_segments[0] in ("server", "server_modules", "certs", "logs", "tools"))
            or lower_path.endswith((".py", ".pyc", ".command", ".sh", ".lua", ".pem", ".key", ".jsonl"))
            or any(seg.startswith(".") and seg != ".well-known" for seg in path_segments)
        ):
            return True
        # env-config.local.js は OBS パスワードを含むため localhost にのみ配信する (Issue #46)。
        #   LAN のページは /api/config（トークン付き）から取得する経路に一本化する。
        #   script タグはヘッダを付けられないため、判定は localhost かどうかで行う。
        if basename == "env-config.local.js" and not self._client_is_local():
            return True
        return False

    def do_HEAD(self):
        # SimpleHTTPRequestHandler.do_HEAD は do_GET のガードを通らないため、
        # 同じ静的ガードを適用してから親実装に委ねる (PR #186 レビュー指摘 1)。
        # 遮断時は HEAD の作法どおり本文を返さない。
        unquoted_path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if self._is_static_blocked(unquoted_path):
            try:
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        super().do_HEAD()

    def list_directory(self, path):
        # ディレクトリ一覧はファイル構成の露出になるため常時 403 とする (Issue #173)。
        # send_head 経由 (GET / HEAD 双方) で呼ばれるため、本文は送らない。
        try:
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        unquoted_path = urllib.parse.unquote(parsed.path)

        # 0-T. OBS WebSocket トンネル (Issue #115)。TLS リスナーでのみ有効で、
        #   それ以外のリスナーでは通常の 404 として扱う。
        if unquoted_path == OBS_WS_TUNNEL_PATH:
            if getattr(self.server, "obs_ws_tunnel_enabled", False):
                self._handle_obs_ws_tunnel()
            else:
                self.send_json({"error": "Not Found"}, 404)
            return

        # 0-0. セキュリティ保護: 設定ファイル・秘密鍵・ログ・Python ソース等への直接HTTPアクセスを遮断
        #   判定本体は _is_static_blocked() に共通化し、do_HEAD からも同じ判定を使う (PR #186 レビュー指摘 1)
        if self._is_static_blocked(unquoted_path):
            self.send_json({"error": "Forbidden"}, 403)
            return

        # 0-A. ブラウザ・iPad Safari が自動要求するアイコンや DevTools メタデータは 204 で応答して 404 ログを防止
        if (
            unquoted_path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")
            or unquoted_path.startswith("/.well-known/")
        ):
            self.send_response(204)
            self.end_headers()
            return

        # 0-A2. iPad Safari のブックマーク互換リダイレクト
        #   - ルート "/" または旧コントローラーURL -> "/Controller/"
        #   - 旧 WebCanvas 単体サーバー時代のパス "/pad.html" -> "/WebCanvas/pad.html"
        if unquoted_path in (
            "/",
            "/index.html",
            "/Controller",
            "/BGMController.html",
            "/Controller/BGMController.html",
            "/Controller/archive/BGMController.html",
            "/Ticker/controller.html"
        ):
            self.send_response(302)
            # Controller 系が無効な配布構成では CommentViewer を既定にする (Issue #150 PR-3)
            self.send_header("Location", "/Controller/" if _CONTROLLER_ENABLED else "/CommentViewer/")
            self.end_headers()
            return

        if unquoted_path in ("/pad.html", "/WebCanvas", "/WebCanvas/"):
            self.send_response(302)
            self.send_header("Location", "/WebCanvas/pad.html")
            self.end_headers()
            return

        # 0-B. わんコメ OneSDK (../../preset/__origin/js/onesdk.js) がローカルサーバー経由で呼ばれた場合の 404 防止スタブ
        if unquoted_path.endswith("/onesdk.js"):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"/* OneSDK stub: running in standalone server mode */\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # 0-C. 旧「Ticker 2」フォルダを参照している OBS ブラウザソースがあった場合、自動で「Ticker」へフォールバック
        if unquoted_path.startswith("/Ticker 2/"):
            rewritten = "/Ticker/" + unquoted_path[len("/Ticker 2/"):]
            self.path = urllib.parse.quote(rewritten, safe="/:@-._~") + (f"?{parsed.query}" if parsed.query else "")
            parsed = urllib.parse.urlparse(self.path)

        # 0-D0. API トークン発行（localhost 限定。iPad へは Mac で表示して手入力する）
        if parsed.path == "/api/auth/token":
            if self._client_is_local():
                self.send_json({"api_token": get_or_create_api_token()})
            else:
                self.send_json({"error": "Forbidden"}, 403)
            return

        # 0-D. 軽量ローカル設定取得エンドポイント（外部API通信を行わず数msで即答・初期ロードのフリーズ防止）
        if parsed.path == "/api/config":
            config = load_twitch_config()
            # Controller 系が無効な構成では LAN 情報を空で返す (Issue #150 PR-3)。
            # クライアント側 (shared/env-config.js) は配列でなければ無視する。
            net_info = get_lan_network_info() if get_lan_network_info is not None else {
                "lan_ips": [], "local_hostname": ""
            }
            self.send_json({
                "status": "ok",
                "configured": bool(config.get("client_id") and config.get("access_token")),
                "channel_name": config.get("channel_name", ""),
                "youtube_handle": config.get("youtube_handle", "@your_channel"),
                "youtube_video_id": config.get("youtube_video_id", ""),
                "broadcaster_id": config.get("broadcaster_id", ""),
                "client_id": config.get("client_id", ""),
                "obs_password": self._visible_obs_password(config),
                "pngtuber_source_name": config.get("pngtuber_source_name", ""),
                "current_ticker": config.get("current_ticker", ""),
                "ticker_speed": config.get("ticker_speed", 80),
                "title_templates": config.get("title_templates"),
                "ticker_presets": config.get("ticker_presets"),
                "x_share_prefix": config.get("x_share_prefix"),
                "viewer_settings": config.get("viewer_settings", DEFAULT_COMMENT_VIEWER_SETTINGS),
                "presets": CATEGORY_PRESETS,
                "lan_ips": net_info["lan_ips"],
                "local_hostname": net_info["local_hostname"]
            })
            return

        # 1. サーバーおよびTwitchステータス確認
        if parsed.path in ("/api/status", "/api/twitch/status"):
            config = load_twitch_config()
            configured = bool(config.get("client_id") and config.get("access_token"))
            channel_info = None
            token_info = validate_twitch_token(config) if configured else {
                "valid": False, "scopes": [], "missing_scopes": REQUIRED_SCOPES
            }

            if configured and token_info.get("valid"):
                # validate_twitch_token 内で自動リフレッシュされた場合に最新トークンを取り込む
                config = load_twitch_config()
                b_id = config.get("broadcaster_id")
                if not b_id and token_info.get("user_id"):
                    b_id = token_info["user_id"]
                    config["broadcaster_id"] = b_id
                    save_twitch_config({"broadcaster_id": b_id})
                elif not b_id and config.get("channel_name"):
                    b_id = get_broadcaster_id_by_login(config["channel_name"], config)
                    if b_id:
                        config["broadcaster_id"] = b_id
                        save_twitch_config({"broadcaster_id": b_id})

                if b_id:
                    c_data, c_code = twitch_request(f"channels?broadcaster_id={b_id}", "GET", config=config)
                    if c_code == 200 and c_data.get("data"):
                        channel_info = c_data["data"][0]

            self.send_json({
                "status": "ok",
                "configured": configured,
                "presets": CATEGORY_PRESETS,
                "channel_info": channel_info,
                "channel_name": config.get("channel_name", ""),
                "youtube_handle": config.get("youtube_handle", "@your_channel"),
                "youtube_video_id": config.get("youtube_video_id", ""),
                "broadcaster_id": config.get("broadcaster_id", ""),
                "client_id": config.get("client_id", ""),
                "obs_password": self._visible_obs_password(config),
                "current_ticker": config.get("current_ticker", ""),
                "ticker_speed": config.get("ticker_speed", 80),
                "title_templates": config.get("title_templates"),
                "ticker_presets": config.get("ticker_presets"),
                "x_share_prefix": config.get("x_share_prefix"),
                "viewer_settings": config.get("viewer_settings", DEFAULT_COMMENT_VIEWER_SETTINGS),
                "token_info": token_info,
                "required_scopes": REQUIRED_SCOPES
            })
            return

        # 2. Twitch OAuth ワンクリック認可開始エンドポイント
        if parsed.path == "/api/twitch/auth":
            config = load_twitch_config()
            client_id = (query.get("client_id", [""])[0] or config.get("client_id", "")).strip()
            if not client_id:
                self.send_html(f"""
                <html><body style="background:#111216;color:#fff;font-family:sans-serif;padding:24px;">
                <h3>⚠️ Client ID が未設定です</h3>
                <p>先に {_settings_guide()}で Client ID を入力・保存してください。</p>
                </body></html>
                """, 400)
                return

            redirect_uri = _oauth_redirect_uri(self.headers.get("Host"), self.server.server_port)
            response_type = "code" if config.get("client_secret", "").strip() else "token"
            scope_str = " ".join(REQUIRED_SCOPES)
            auth_params = urllib.parse.urlencode({
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": response_type,
                "scope": scope_str,
                "force_verify": "true",
                "state": _issue_oauth_state()
            })
            auth_url = f"https://id.twitch.tv/oauth2/authorize?{auth_params}"
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        # 3. Twitch OAuth コールバック（Codeフロー ＆ Implicit フロー両対応）
        if parsed.path == "/api/twitch/callback":
            code = query.get("code", [""])[0]
            if code:
                # OAuth state の照合 (Issue #92)。一致しなければトークン交換に進まない
                if not _consume_oauth_state(query.get("state", [""])[0]):
                    self.send_html(f"""
                    <html><body style="background:#111216;color:#f87171;font-family:sans-serif;padding:24px;">
                    <h3>⚠️ 認可の検証に失敗しました</h3>
                    <p style="color:#cbd5e1;">認可フローの state が一致しませんでした。{_oauth_retry_guide_prefix()}認可を最初からやり直してください。</p>
                    </body></html>
                    """, 400)
                    return
                config = load_twitch_config()
                client_id = config.get("client_id", "").strip()
                client_secret = config.get("client_secret", "").strip()
                redirect_uri = _oauth_redirect_uri(self.headers.get("Host"), self.server.server_port)
                token_params = urllib.parse.urlencode({
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                }).encode("utf-8")
                try:
                    req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=token_params, method="POST")
                    with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=10) as resp:
                        t_data = json.loads(resp.read().decode("utf-8"))
                        # チャンネル一致チェック (Issue #99): 設定済み broadcaster と
                        # 異なるユーザーのトークンは保存しない
                        verdict, token_user_id = _check_token_channel_match(
                            t_data.get("access_token", ""), config.get("broadcaster_id"))
                        if verdict == "mismatch":
                            log_event("twitch-token.channel-mismatch", {
                                "source": "oauth_callback",
                                "configured_broadcaster_id": str(config.get("broadcaster_id")),
                                "token_user_id": token_user_id,
                            })
                            self.send_html(f"""
                            <html><body style="background:#111216;color:#f87171;font-family:sans-serif;padding:24px;">
                            <h3>⚠️ トークンの保存を中止しました</h3>
                            <p style="color:#cbd5e1;">認可された Twitch アカウントが、設定済みのチャンネルと一致しません。設定済みのチャンネルと同じアカウントでログインし、{_oauth_retry_guide_prefix()}認可をやり直してください。</p>
                            </body></html>
                            """, 403)
                            return
                        if verdict == "unreachable":
                            log_event("twitch-token.channel-check-skipped", {
                                "source": "oauth_callback",
                                "reason": "validation_unreachable",
                            })
                        save_twitch_config({
                            "access_token": t_data.get("access_token", ""),
                            "refresh_token": t_data.get("refresh_token", "")
                        })
                        self.send_html("""
                        <html><body style="background:#111216;color:#10b981;font-family:sans-serif;padding:32px;text-align:center;">
                        <h2>✅ Twitch 全スコープ認可＆トークン保存が完了しました！</h2>
                        <p style="color:#cbd5e1;">このタブを閉じて、Controller または CommentViewer に戻ってください。</p>
                        <script>
                          try {
                            const bc = new BroadcastChannel('obs_widgets_twitch_auth_v1');
                            bc.postMessage({ type: 'auth_updated', timestamp: Date.now() });
                          } catch (e) {}
                          setTimeout(()=>window.close(), 1500);
                        </script>
                        </body></html>
                        """)
                        return
                except Exception as e:
                    self.send_html(f"<html><body style='background:#111;color:#f87171;padding:24px;'><h3>認可エラー: {html.escape(str(e))}</h3></body></html>", 500)
                    return

            self.send_html("""
            <!DOCTYPE html>
            <html lang="ja">
            <head><meta charset="UTF-8"><title>Twitch 認証完了処理中...</title></head>
            <body style="background:#111216;color:#efeff1;font-family:sans-serif;padding:32px;text-align:center;">
              <h2 id="msg">🔄 トークンを保存しています...</h2>
              <p id="sub" style="color:#8c909c;font-size:14px;"></p>
              <script>
                (async () => {
                  const hash = window.location.hash.substring(1);
                  const params = new URLSearchParams(hash);
                  const token = params.get('access_token');
                  const oauthState = params.get('state');
                  if (!token) {
                    document.getElementById('msg').textContent = '⚠️ トークンが見つかりませんでした';
                    document.getElementById('sub').textContent = 'このページのアドレス (/api/twitch/callback まで) が、Twitch開発者コンソールの OAuth Redirect URLs に登録されているか確認してください。';
                    return;
                  }
                  const res = await fetch('/api/twitch/save-config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ access_token: token, oauth_state: oauthState })
                  });
                  if (res.ok) {
                    document.getElementById('msg').style.color = '#10b981';
                    document.getElementById('msg').textContent = '✅ Twitch 全スコープ認可が完了しました！';
                    document.getElementById('sub').textContent = '自動的にウィンドウを閉じます...';
                    try {
                      const bc = new BroadcastChannel('obs_widgets_twitch_auth_v1');
                      bc.postMessage({ type: 'auth_updated', timestamp: Date.now() });
                    } catch (e) {}
                    setTimeout(() => window.close(), 1200);
                  } else {
                    document.getElementById('msg').textContent = '❌ 保存に失敗しました';
                  }
                })();
              </script>
            </body>
            </html>
            """)
            return

        # 4. VOICEVOX ステータス＆話者一覧取得
        if parsed.path in ("/api/voicevox/status", "/api/voicevox/speakers"):
            data, code = fetch_voicevox_speakers()
            self.send_json(data, code)
            return

        # 5. 音声認識字幕ちゃん連動：現在の翻訳言語ステータス取得 (ハートビート自動記録)
        if parsed.path == "/api/subtitle/lang":
            if record_heartbeat is not None:
                record_heartbeat("jimakuchan")
            self.send_json(get_current_subtitle_lang_status())
            return

        # 5-A. アバター配置の共有データ取得 (Issue #170)
        if parsed.path == "/api/avatar-layouts":
            if not _AVATAR_LAYOUTS_ENABLED:
                self.send_json({"error": "avatar layouts are not available"}, 404)
                return
            self.send_json(_load_avatar_layouts())
            return

        # 5-A2. feed ログのセッション状態取得 (Issue #179)。
        #       配信中に CommentViewer がリロードされても、クライアントが
        #       記録状態を復元できるようサーバー側の状態を返す。
        if parsed.path == "/api/feed-log/status":
            self.send_json({"session_open": feed_log_session_open()})
            return

        # 5-B. クライアント死活監視ハートビートエンドポイント
        #     obs=1/0 が付いている場合は、そのクライアントの OBS WebSocket 接続状態も記録する
        if parsed.path == "/api/heartbeat":
            if record_heartbeat is None:
                # Controller 系が無効な構成ではハートビート受け口を持たない (Issue #150 PR-3)
                self.send_json({"error": "Not Found"}, 404)
                return
            client_name = (query.get("client", [""])[0] or "").strip().lower()
            obs_raw = (query.get("obs", [""])[0] or "").strip()
            obs_connected = {"1": True, "0": False}.get(obs_raw)
            if client_name:
                record_heartbeat(client_name, obs_connected)
            self.send_json({"status": "ok", "client": client_name})
            return

        # 6. YouTube Live チャット＆アラート取得ポーリング
        if parsed.path == "/api/youtube/poll":
            try:
                since_seq = int(query.get("since", ["0"])[0])
            except Exception:
                since_seq = 0
            config = load_twitch_config()
            res = YOUTUBE_MANAGER.touch_client_poll(since_seq)
            res["youtube_handle"] = config.get("youtube_handle", "@your_channel")
            res["youtube_video_id"] = config.get("youtube_video_id", "")
            self.send_json(res)
            return

        # 7. ミニドック用：周辺アプリ（VOICEVOX・veadotube mini・字幕ちゃん・YouTube）稼働ステータス一括取得
        if parsed.path == "/api/launcher/status":
            if get_all_launcher_status is None:
                self.send_json({"error": "Not Found"}, 404)
                return
            config = load_twitch_config()
            self.send_json(get_all_launcher_status(config))
            return

        # 8. PNGTuber: OBS 音声レベル SSE ストリーム・単発レベル・設定・設定画像取得 (読み取り専用、任意モジュール)
        if parsed.path == "/api/pngtuber/stream":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            avatar = (query.get("avatar", [""])[0] or "").strip()
            if not _register_pngtuber_stream(avatar=avatar):
                try:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Retry-After", "2")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "too many sse streams"}).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.close_connection = True
            try:
                try:
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()

                last_seq = None
                while not _is_pngtuber_stopped():
                    changed, last_seq, payload = _wait_pngtuber_stream_update(
                        last_seq=last_seq,
                        timeout=2.0,
                        avatar=avatar,
                    )
                    if _is_pngtuber_stopped():
                        break
                    if changed and payload is not None:
                        chunk = f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
                    else:
                        chunk = b": keepalive\n\n"
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _unregister_pngtuber_stream(avatar=avatar)
            return

        if parsed.path == "/api/pngtuber/level":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            avatar = (query.get("avatar", [""])[0] or "").strip()
            self.send_json(_get_pngtuber_level(avatar=avatar))
            return

        if parsed.path == "/api/pngtuber/config":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            avatar = (query.get("avatar", [""])[0] or "").strip()
            self.send_json(_get_pngtuber_client_config(avatar=avatar))
            return

        if parsed.path == "/api/pngtuber/inputs":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not self._can_write():
                self.send_json({"error": "unauthorized: API token required"}, 401)
                return
            refresh = (query.get("refresh", [""])[0] or "").strip().lower() in ("1", "true")
            self.send_json(_list_obs_audio_inputs(refresh=refresh))
            return

        if parsed.path == "/api/pngtuber/images":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            self.send_json({"images": _list_pngtuber_images()})
            return

        if parsed.path == "/api/pngtuber/mic-assignment":
            if not _PNGTUBER_MIC_ASSIGNMENT_ENABLED or not callable(_get_pngtuber_mic_assignment):
                self.send_json({"error": "Not Found"}, 404)
                return
            res = _get_pngtuber_mic_assignment()
            if isinstance(res, tuple) and len(res) == 2:
                self.send_json(res[0], res[1])
            else:
                self.send_json(res)
            return

        if unquoted_path.startswith("/api/pngtuber/image/"):
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            avatar = (query.get("avatar", [""])[0] or "").strip()
            slot = unquoted_path[len("/api/pngtuber/image/"):]
            filepath, content_type = _resolve_pngtuber_image_file(slot, avatar=avatar)
            if not filepath or not content_type:
                self.send_json({"error": "Not Found"}, 404)
                return
            try:
                with open(filepath, "rb") as f:
                    img_bytes = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(img_bytes)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_json({"error": "Not Found"}, 404)
            return

        # 9. CubaseBridge: テロップ状態 SSE ストリーム・単発状態 (読み取り専用、任意モジュール)
        if parsed.path == "/api/cubase-bridge/stream":
            if not _CUBASE_BRIDGE_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not _register_cubase_stream():
                try:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Retry-After", "2")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "too many sse streams"}).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.close_connection = True
            try:
                try:
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()

                last_seq = None
                last_visible = None
                while not _is_cubase_stopped():
                    changed, last_seq, payload = _wait_cubase_stream_update(
                        last_seq=last_seq,
                        timeout=2.0,
                        last_visible=last_visible,
                    )
                    if payload is not None:
                        last_visible = payload["visible"]
                    if changed and payload is not None:
                        chunk = f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
                    else:
                        chunk = b": keepalive\n\n"
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _unregister_cubase_stream()
            return

        if parsed.path == "/api/cubase-bridge/state":
            if not _CUBASE_BRIDGE_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            self.send_json(_get_cubase_state())
            return

        # 10. VoiceCommand: 設定・OBS リソース・実行履歴取得 (認証必須)
        if parsed.path in (
            "/api/voice-command/config",
            "/api/voice-command/obs-resources",
            "/api/voice-command/history",
        ):
            if not _VOICE_COMMAND_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not self._can_write():
                self.send_json({"error": "unauthorized: API token required"}, 401)
                return
            if parsed.path == "/api/voice-command/config":
                self.send_json(_load_voice_command_config())
            elif parsed.path == "/api/voice-command/obs-resources":
                self.send_json(_get_voice_command_obs_resources())
            else:
                self.send_json({"history": _get_voice_command_history()})
            return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        unquoted_path = urllib.parse.unquote(parsed.path)
        # ボディ読み込みの堅牢化 (Issue #86): Content-Length の防御的パース、上限、
        # 読み込みタイムアウト。不正な入力でハンドラスレッドを落とさない。
        raw_length = self.headers.get("Content-Length", "")
        try:
            content_length = int(raw_length) if raw_length else 0
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0:
            self.send_json({"error": "Invalid Content-Length"}, 400)
            return

        # PNGTuber 画像アップロード (バイナリ / multipart 対応、UTF-8 JSON パースの前に処理)
        if unquoted_path.startswith("/api/pngtuber/image/"):
            if not _PNGTUBER_ENABLED:
                self.close_connection = True
                self.send_json({"error": "Not Found"}, 404)
                return
            if not self._can_write():
                self.close_connection = True
                self.send_json({"error": "unauthorized: API token required"}, 401)
                return
            slot = unquoted_path[len("/api/pngtuber/image/"):]
            if slot not in _PNGTUBER_IMAGE_SLOTS:
                self.close_connection = True
                self.send_json({"error": f"Invalid image slot: {slot}"}, 400)
                return
            if content_length == 0:
                self.send_json({"error": "Empty request body"}, 400)
                return
            if content_length > _MAX_PNGTUBER_IMAGE_BYTES + 65536:
                self.close_connection = True
                self.send_json({"error": "Image file too large"}, 413)
                return
            try:
                self.connection.settimeout(self.POST_READ_TIMEOUT)
                raw_body = self.rfile.read(content_length)
            except Exception:
                self.close_connection = True
                self.send_json({"error": "Failed to read request body"}, 400)
                return
            content_type = (self.headers.get("Content-Type") or "").strip()
            try:
                if content_type.lower().startswith("multipart/form-data"):
                    filename, img_bytes = _parse_multipart_file_upload(content_type, raw_body)
                else:
                    filename = (
                        (query.get("filename", [""])[0] or "").strip()
                        or (self.headers.get("X-Filename") or "").strip()
                    )
                    img_bytes = raw_body
                raw_query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                if "avatar" in raw_query:
                    upload_avatar = (raw_query["avatar"][0] or "").strip()
                    if not upload_avatar:
                        raise ValueError("avatar must not be empty")
                else:
                    upload_avatar = None
                saved = _save_pngtuber_image_upload(
                    slot, filename, img_bytes, avatar=upload_avatar
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except OSError:
                self.send_json({"error": "Failed to save image"}, 500)
                return
            self.send_json({
                "status": "uploaded",
                "slot": saved["slot"],
                "filename": saved["filename"],
                "path": saved["path"],
                "config": _get_pngtuber_client_config(avatar=upload_avatar),
            })
            return

        if content_length > self.MAX_POST_BODY:
            self.close_connection = True
            self.send_json({"error": "Request body too large"}, 413)
            return
        body_text = "{}"
        if content_length > 0:
            try:
                self.connection.settimeout(self.POST_READ_TIMEOUT)
                body_text = self.rfile.read(content_length).decode("utf-8")
            except Exception:
                self.close_connection = True
                self.send_json({"error": "Failed to read request body"}, 400)
                return
        json_valid = True
        try:
            req_data = json.loads(body_text)
        except Exception:
            req_data = {}
            json_valid = False

        # 0. 状態変更 POST の認証 (Issue #46 / #58)。localhost 発信か有効なトークンが必須。
        #    対象外: /api/translate (無状態の計算のみで状態変更を伴わない)
        #    例外 (Issue #136): /api/twitch/save-config の Implicit フロー保存は、
        #    有効な oauth_state (サーバー発行・単回・10分) の提示をもって認可済み
        #    とみなす。コールバックページは LAN 上のブラウザでも動くため API
        #    トークンを持てず、code フローのコールバック (GET + state) と同等の
        #    担保に揃える。照合に成功した場合はここで消費し、後段の再照合を避ける。
        oauth_state_ok = False
        if parsed.path == "/api/twitch/save-config" and isinstance(req_data, dict) and "oauth_state" in req_data:
            if _consume_oauth_state(str(req_data.get("oauth_state") or "")):
                oauth_state_ok = True
                req_data.pop("oauth_state", None)
        if parsed.path in (
            "/api/launcher/action",
            "/api/twitch/save-config",
            "/api/twitch/set-category",
            "/api/twitch/set-title",
            "/api/twitch/eventsub/subscribe",
            "/api/youtube/reconnect",
            "/api/youtube/test",
            "/api/subtitle/lang",
            "/api/avatar-layouts",
            "/api/cubase-bridge/event",
            "/api/cubase-bridge/clear",
            "/api/pngtuber/config",
            "/api/pngtuber/mic-assignment",
            "/api/voicevox/tts",
            "/api/voicevox/synthesis",
            "/api/feed-log",
            "/api/voice-command/config",
            "/api/voice-command/utterance",
            "/api/voice-command/test",
        ) and not self._can_write() and not oauth_state_ok:
            self.send_json({"error": "unauthorized: API token required"}, 401)
            return

        if parsed.path in (
            "/api/feed-log",
            "/api/twitch/save-config",
            "/api/twitch/eventsub/subscribe",
            "/api/translate",
            "/api/subtitle/lang",
            "/api/youtube/reconnect",
            "/api/youtube/test",
            "/api/launcher/action",
            "/api/voicevox/tts",
            "/api/voicevox/synthesis",
            "/api/voice-command/config",
            "/api/voice-command/utterance",
            "/api/voice-command/test",
        ) and (not json_valid or not isinstance(req_data, dict)):
            self.send_json({"error": "Invalid JSON body"}, 400)
            return

        # 0.4 VoiceCommand: 設定保存・確定発話受付・テスト実行
        if parsed.path == "/api/voice-command/config":
            if not _VOICE_COMMAND_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            try:
                saved = _save_voice_command_config(req_data)
                self.send_json({"ok": True, "config": saved})
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            return

        if parsed.path in ("/api/voice-command/utterance", "/api/voice-command/test"):
            if not _VOICE_COMMAND_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            text = req_data.get("text")
            if not isinstance(text, str):
                self.send_json({"error": "text must be a string"}, 400)
                return
            is_test = parsed.path == "/api/voice-command/test"
            self.send_json(_process_voice_command_utterance(text, is_test=is_test))
            return

        # 0.5 コメント・アラート履歴の追記 (UC-09)。単一レコード dict か
        #     {"records": [...]} のバッチを受け付け、記録できた件数を返す。
        if parsed.path == "/api/feed-log":
            records = req_data.get("records")
            if not isinstance(records, list):
                records = [req_data]
            self.send_json({"recorded": feed_log_record_events(records)})
            return

        # CubaseBridge: 受け口からのイベント取り込み (任意モジュール)
        if parsed.path == "/api/cubase-bridge/event":
            if not _CUBASE_BRIDGE_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not json_valid or not isinstance(req_data, dict):
                self.send_json({"error": "Invalid JSON body"}, 400)
                return
            accepted = _ingest_cubase_event(req_data)
            self.send_json({"accepted": bool(accepted)})
            return

        # CubaseBridge: 画面表示のみをクリア (任意モジュール、clear_display 未定義時は 404)
        if parsed.path == "/api/cubase-bridge/clear":
            if not _CUBASE_BRIDGE_ENABLED or _clear_cubase_display is None:
                self.send_json({"error": "Not Found"}, 404)
                return
            result = _clear_cubase_display()
            if isinstance(result, dict):
                self.send_json(result)
            else:
                self.send_json({"ok": True})
            return

        # PNGTuber: 設定のマージ保存（対象外のアバター・未知キーは保持）
        if parsed.path == "/api/pngtuber/config":
            if not _PNGTUBER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not json_valid or not isinstance(req_data, dict):
                self.send_json({"error": "Invalid JSON body"}, 400)
                return
            if "avatar" in req_data:
                avatar_raw = req_data.pop("avatar")
                if avatar_raw is None:
                    save_avatar = None
                elif not isinstance(avatar_raw, str):
                    self.send_json({"error": "avatar must be a string"}, 400)
                    return
                else:
                    save_avatar = avatar_raw.strip()
                    if not save_avatar:
                        self.send_json({"error": "avatar must not be empty"}, 400)
                        return
            else:
                save_avatar = None
            try:
                _save_pngtuber_config(req_data, avatar=save_avatar)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except OSError:
                self.send_json({"error": "Failed to save pngtuber_config.json"}, 500)
                return
            self.send_json({
                "status": "saved",
                "config": _get_pngtuber_client_config(avatar=save_avatar),
            })
            return

        # 1. 設定の保存
        if parsed.path == "/api/twitch/save-config":
            # Implicit フローのコールバックページは oauth_state を付けて送ってくる。
            # 付いている場合は必ず照合し、設定としては保存しない (Issue #92)
            if "oauth_state" in req_data:
                oauth_state = req_data.pop("oauth_state")
                if not _consume_oauth_state(str(oauth_state or "")):
                    self.send_json({"error": "OAuth state mismatch"}, 403)
                    return
            # oauth_state のみで認可された保存 (LAN のコールバックページ経由) は、
            # 検証済み access_token の単独保存に限る (Issue #174)。state は無認証の
            # /api/twitch/auth から誰でも取得できるため、それ以外のキーや検証できない
            # トークンを含む保存は拒否する。認証済み経路の fail-open とは別扱いで、
            # この経路は fail-closed とする。
            if oauth_state_ok and not self._can_write():
                extra_keys = [k for k in req_data if k != "access_token"]
                token_candidate = str(req_data.get("access_token") or "")
                if extra_keys or not token_candidate:
                    self.send_json({"error": "oauth_state save accepts access_token only"}, 403)
                    return
                token_check = validate_twitch_token({"access_token": token_candidate})
                if not token_check.get("valid"):
                    log_event("twitch-token.state-save-rejected", {
                        "source": "save_config",
                        "reason": "token_invalid_or_unreachable",
                    })
                    self.send_json({"error": "Access token could not be validated"}, 403)
                    return
            # チャンネル一致チェック (Issue #99): access_token を含む保存では、
            # 設定済み broadcaster と異なるユーザーの有効なトークンを拒否する
            incoming_token = req_data.get("access_token")
            if incoming_token:
                configured_bid = load_twitch_config().get("broadcaster_id")
                verdict, token_user_id = _check_token_channel_match(
                    str(incoming_token), configured_bid)
                if verdict == "mismatch":
                    log_event("twitch-token.channel-mismatch", {
                        "source": "save_config",
                        "configured_broadcaster_id": str(configured_bid),
                        "token_user_id": token_user_id,
                    })
                    self.send_json({"error": "Access token belongs to a different Twitch account"}, 403)
                    return
                if verdict == "unreachable":
                    log_event("twitch-token.channel-check-skipped", {
                        "source": "save_config",
                        "reason": "validation_unreachable",
                    })
            config = save_twitch_config(req_data)
            b_id = config.get("broadcaster_id")
            token_info = validate_twitch_token(config)
            if token_info.get("valid") and token_info.get("user_id"):
                b_id = token_info["user_id"]
                save_twitch_config({"broadcaster_id": b_id})
            elif not b_id and config.get("channel_name"):
                b_id = get_broadcaster_id_by_login(config["channel_name"], config)
                if b_id:
                    save_twitch_config({"broadcaster_id": b_id})

            self.send_json({
                "status": "saved",
                "broadcaster_id": b_id or "",
                "token_info": token_info,
                "viewer_settings": config.get("viewer_settings", DEFAULT_COMMENT_VIEWER_SETTINGS)
            })
            return

        # 2. カテゴリ・配信情報の変更 (Controller 専用機能: 無効な構成では 404, Issue #150 PR-3)
        if parsed.path in ("/api/twitch/set-category", "/api/twitch/set-title"):
            if not _CONTROLLER_ENABLED:
                self.send_json({"error": "Not Found"}, 404)
                return
            if not json_valid or not isinstance(req_data, dict):
                self.send_json({"error": "Invalid JSON body"}, 400)
                return
            config = load_twitch_config()
            b_id = config.get("broadcaster_id")
            if not b_id and config.get("channel_name"):
                b_id = get_broadcaster_id_by_login(config["channel_name"], config)
                if b_id:
                    config["broadcaster_id"] = b_id
                    save_twitch_config({"broadcaster_id": b_id})

            if not b_id:
                self.send_json({"error": "Twitch設定が未完了です。Client IDとAccess Tokenを保存してください。"}, 400)
                return

            preset_key = req_data.get("preset")
            game_id = req_data.get("game_id")
            title = req_data.get("title")

            if preset_key and preset_key in CATEGORY_PRESETS:
                preset = CATEGORY_PRESETS[preset_key]
                game_id = preset["game_id"]
                if req_data.get("update_title") and not title:
                    title = preset.get("default_title")

            patch_body = {}
            if game_id:
                patch_body["game_id"] = str(game_id)
            if title is not None and str(title).strip() != "":
                patch_body["title"] = str(title).strip()

            if not patch_body:
                self.send_json({"error": "game_id または title を指定してください。"}, 400)
                return

            res_data, code = twitch_request(f"channels?broadcaster_id={b_id}", method="PATCH", body=patch_body, config=config)

            if code in (200, 204):
                c_data, _ = twitch_request(f"channels?broadcaster_id={b_id}", "GET", config=config)
                latest = c_data["data"][0] if (c_data and c_data.get("data")) else None
                self.send_json({
                    "status": "success",
                    "message": "配信情報を更新しました！",
                    "channel_info": latest
                })
            else:
                self.send_json(res_data, code)
            return

        # 3. EventSub WebSocket サブスクリプション一括登録プロキシ
        if parsed.path == "/api/twitch/eventsub/subscribe":
            session_id = (req_data.get("session_id") or "").strip()
            if not session_id:
                self.send_json({"error": "session_id が必要です。"}, 400)
                return

            config = load_twitch_config()
            token_info = validate_twitch_token(config)
            if not token_info.get("valid"):
                if "error" in token_info:
                    self.send_json({
                        "error": "Twitch トークン検証サーバーへの接続に失敗しました。",
                        "token_info": token_info
                    }, 502)
                else:
                    self.send_json({
                        "error": "Access Token が無効または期限切れです。",
                        "token_info": token_info
                    }, 401)
                return

            # validate_twitch_token 内で自動リフレッシュされた場合に最新トークンを取り込む
            config = load_twitch_config()
            user_id = token_info.get("user_id") or config.get("broadcaster_id")
            b_id = config.get("broadcaster_id") or user_id
            if not b_id and config.get("channel_name"):
                b_id = get_broadcaster_id_by_login(config["channel_name"], config)
            if b_id and b_id != config.get("broadcaster_id"):
                save_twitch_config({"broadcaster_id": b_id})

            results = register_eventsub_subscriptions(session_id, b_id, user_id, config)
            self.send_json({
                "status": "ok",
                "broadcaster_id": b_id,
                "results": results,
                "token_info": token_info
            })
            return

        # 4. 外国語コメントの日本語自動翻訳エンドポイント
        if parsed.path == "/api/translate":
            # レート制限 (Issue #95)。超過時は 429 で返し、クライアントは
            # 後段のフォールバックを経て原文表示に劣化する
            if not _check_translate_rate(self._client_ip()):
                self.send_json({"error": "rate_limited", "is_translated": False}, 429)
                return
            text = req_data.get("text", "")
            result = translate_to_japanese(text)
            self.send_json(result)
            return

        # 5. 音声認識字幕ちゃん連動：外国語コメント検出時の翻訳言語更新（5分タイマーリセット）
        if parsed.path == "/api/subtitle/lang":
            self.send_json(update_subtitle_lang_state(req_data))
            return

        # 5-A. アバター配置の共有データ保存 (Issue #170)
        if parsed.path == "/api/avatar-layouts":
            if not _AVATAR_LAYOUTS_ENABLED:
                self.send_json({"error": "avatar layouts are not available"}, 404)
                return
            if not json_valid or not isinstance(req_data, dict):
                self.send_json({"error": "Invalid JSON body"}, 400)
                return
            self.send_json(_save_avatar_layouts(req_data))
            return

        # 5-B. PNGTuber 表示モード連動のマイク割当更新 (任意拡張フック, Issue #9)
        if parsed.path == "/api/pngtuber/mic-assignment":
            if not _PNGTUBER_MIC_ASSIGNMENT_ENABLED or not callable(_update_pngtuber_mic_assignment):
                self.send_json({"error": "Not Found"}, 404)
                return
            if not json_valid or not isinstance(req_data, dict):
                self.send_json({"error": "Invalid JSON body"}, 400)
                return
            try:
                res = _update_pngtuber_mic_assignment(req_data)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            if isinstance(res, tuple) and len(res) == 2:
                self.send_json(res[0], res[1])
            else:
                self.send_json(res)
            return

        # 6. YouTube Live 設定保存・即時再接続
        if parsed.path == "/api/youtube/reconnect":
            save_payload = {}
            if "youtube_handle" in req_data:
                save_payload["youtube_handle"] = str(req_data["youtube_handle"]).strip() or "@your_channel"
            if "youtube_video_id" in req_data:
                save_payload["youtube_video_id"] = str(req_data["youtube_video_id"]).strip()
            if save_payload:
                save_twitch_config(save_payload)
            YOUTUBE_MANAGER.force_reconnect()
            self.send_json({"status": "reconnecting", **save_payload})
            return

        # 6-B. YouTube Live テスト発火（InnerTube生アクション重複注入 → /api/youtube/poll 経由で検証）
        if parsed.path == "/api/youtube/test":
            kind = str(req_data.get("kind") or "").strip()
            res_info = YOUTUBE_MANAGER.inject_test_innertube_event(kind)
            self.send_json({"status": "ok", "kind": kind, **res_info})
            return

        # 6-C. ミニドック用：周辺アプリの一括起動・一括終了・個別起動/停止
        if parsed.path == "/api/launcher/action":
            action_name = str(req_data.get("action") or "").strip().lower()
            if execute_launcher_action is None:
                # Controller 系が無効な配布構成 (Issue #150 PR-3, page_ready は案A):
                # ウィジェットページからの読み込み完了合図だけは 200 で受け流し
                # (browser_refresh があれば記録し、無ければ no-op)、他は 404。
                if action_name == "page_ready":
                    page = str(req_data.get("page") or "")
                    if _note_page_ready is not None:
                        res = _note_page_ready(page)
                        self.send_json({"status": "ok", "action": "page_ready", "result": res})
                    else:
                        self.send_json({"ok": True, "page": page, "session_running": False})
                else:
                    self.send_json({"error": "Not Found"}, 404)
                return
            res = execute_launcher_action(req_data)
            if isinstance(res, tuple):
                self.send_json(res[0], res[1])
            else:
                # state の付与は集約モジュール (controller_status) を import する
                # handler 側で行う (launcher は集約に依存しない。Issue #150)。
                # page_ready は従来どおり state を付けない。
                if res.get("status") == "ok" and action_name != "page_ready" and get_all_launcher_status is not None:
                    res["state"] = get_all_launcher_status()
                self.send_json(res)
            return

        # 7. VOICEVOX 音声合成 (WAVバイナリ返却)
        if parsed.path in ("/api/voicevox/tts", "/api/voicevox/synthesis"):
            text = (req_data.get("text") or "").strip()
            if not text:
                self.send_json({"error": "text が空です"}, 400)
                return

            config = load_twitch_config()
            vs = config.get("viewer_settings") or {}
            speaker_id = req_data.get("speaker_id", vs.get("speaker_id", 3))
            speed_scale = req_data.get("speed_scale", vs.get("tts_speed", 1.05))
            volume_scale = req_data.get("volume_scale", vs.get("tts_volume", 1.0))

            try:
                wav_bytes = synthesize_voicevox_wav(
                    text=text,
                    speaker_id=speaker_id,
                    speed_scale=speed_scale,
                    volume_scale=volume_scale,
                    config=config
                )
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except Exception as e:
                print(f"VOICEVOX synthesis error: {e}")
                self.send_json({
                    "error": f"VOICEVOX音声合成エラー: {e}"
                }, 502)
            return

        self.send_json({"error": "Not Found"}, 404)
