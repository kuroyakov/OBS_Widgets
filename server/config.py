"""
設定管理・定数モジュール (server/config.py)
- サーバーポート・ベースディレクトリ・SSLコンテキスト管理
- Twitch OAuth スコープ定義・カテゴリプリセット定義
- twitch_config.json のスレッドセーフな読み込み・アトミック保存・空文字上書き保護
"""

import os
import re
import ssl
import sys
import copy
import json
import secrets
import threading
import time

DEFAULT_PORT = 8080
DEFAULT_TLS_PORT = 8443
ENV_PORT = "OBS_WIDGETS_PORT"
ENV_TLS_PORT = "OBS_WIDGETS_TLS_PORT"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# TLS (Issue #94 フェーズ1)。証明書と秘密鍵の両方が certs/ に置かれた場合のみ、
# server.py が HTTPS リスナー (TLS_PORT) を追加で起動する。無い場合は従来通り
# HTTP のみで動作する。certs/ は .gitignore 対象 (秘密鍵をコミットしない)。
TLS_CERT_FILE = os.path.join(BASE_DIR, "certs", "tls-cert.pem")
TLS_KEY_FILE = os.path.join(BASE_DIR, "certs", "tls-key.pem")


def is_tls_configured(cert_file=TLS_CERT_FILE, key_file=TLS_KEY_FILE):
    """証明書と秘密鍵の両方が存在し、TLS リスナーが有効になる構成かを返す。"""
    return os.path.isfile(cert_file) and os.path.isfile(key_file)


def resolve_port(env_name, default, environ=None):
    """環境変数から待受ポートを解決・検証する (Issue #45)。

    未設定または空白のみの場合は default を返す。
    設定されている場合は 1〜65535 の整数のみ許可し、不正値は ValueError を送出する。
    """
    env = os.environ if environ is None else environ
    raw_value = env.get(env_name)
    if raw_value is None:
        return default
    raw = str(raw_value).strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError(f"{env_name} は 1〜65535 の整数で指定してください: {raw_value!r}")
    port = int(raw)
    if port < 1 or port > 65535:
        raise ValueError(f"{env_name} は 1〜65535 の範囲で指定してください: {port}")
    return port


def resolve_ports(environ=None, tls_enabled=None):
    """HTTP / TLS の待受ポートを解決し、TLS 有効時は重複がないことを検証して返す。"""
    port = resolve_port(ENV_PORT, DEFAULT_PORT, environ=environ)
    tls_port = resolve_port(ENV_TLS_PORT, DEFAULT_TLS_PORT, environ=environ)
    if tls_enabled is None:
        tls_enabled = is_tls_configured()
    if tls_enabled and port == tls_port:
        raise ValueError(f"{ENV_PORT} と {ENV_TLS_PORT} に同じポート ({port}) は指定できません")
    return port, tls_port


PORT, TLS_PORT = resolve_ports()

# OBS WebSocket トンネル (Issue #115)。HTTPS で開いたページからは平文 ws:// へ
# 接続できない (混合コンテンツ) ため、TLS リスナーにだけ WebSocket トンネルの
# パスを用意し、サーバーが OBS WebSocket (ループバック) へバイト列を中継する。
OBS_WS_TUNNEL_PATH = "/obs-ws"
OBS_WS_UPSTREAM_HOST = "127.0.0.1"
OBS_WS_UPSTREAM_PORT = 4455
CONFIG_FILE = os.path.join(BASE_DIR, "twitch_config.json")

_CONFIG_LOCK = threading.RLock()

# 空文字や空白のみで既存の設定値を誤って消去しないように保護する重要キー
_PROTECTED_NON_EMPTY_KEYS = (
    "client_id",
    "client_secret",
    "access_token",
    "refresh_token",
    "obs_password",
    "broadcaster_id",
    "channel_name",
    "api_token",
)

# OS 別の既知 CA バンドル候補 (Issue #58: certifi 不在の素の Python 向けフォールバック)
_OS_CA_BUNDLE_CANDIDATES = {
    "darwin": (
        "/etc/ssl/cert.pem",
        "/opt/homebrew/etc/ca-certificates/cert.pem",
        "/usr/local/etc/ca-certificates/cert.pem",
    ),
    "linux": (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    ),
}


def _try_load_ca_file(ctx, path):
    """CA バンドルファイルを読み込み、エラー時は例外を出さずエラー文字列を返す。"""
    if not path:
        return "empty CA bundle path"
    try:
        if not os.path.isfile(path):
            return f"CA bundle not found: {path}"
        if os.path.getsize(path) <= 0:
            return f"CA bundle is empty: {path}"
        ctx.load_verify_locations(cafile=path)
        return None
    except Exception as e:
        return f"{path}: {e}"


def build_outbound_ssl_context(platform=None, candidates=None, certifi_module=None):
    """サーバー発 HTTPS 用の SSLContext と CA ストア確保メタデータを構築する (Issue #58)。

    解決順: 既定ストア (SSL_CERT_FILE / システムストア) → certifi → OS 候補パス。
    証明書検証は常時有効 (CERT_REQUIRED / check_hostname=True) とし、
    検証を無効化するフォールバックは絶対に置かない (S-07)。
    """
    target_platform = sys.platform if platform is None else platform
    last_error = None
    source = "none"

    ctx = ssl.create_default_context()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True

    if len(ctx.get_ca_certs()) > 0:
        source = "system_default"
    else:
        # 2. certifi フォールバック
        try:
            mod = certifi_module
            if mod is None:
                import certifi as mod  # type: ignore
            where_fn = getattr(mod, "where", None)
            certifi_path = where_fn() if callable(where_fn) else None
            err = _try_load_ca_file(ctx, certifi_path)
            if err is not None:
                last_error = err
            elif len(ctx.get_ca_certs()) > 0:
                source = "certifi"
        except Exception as e:
            last_error = f"certifi unavailable: {e}"

    # 3. OS 候補パスフォールバック
    if len(ctx.get_ca_certs()) == 0:
        os_candidates = (
            _OS_CA_BUNDLE_CANDIDATES.get(target_platform, ())
            if candidates is None
            else candidates
        )
        for path in os_candidates:
            err = _try_load_ca_file(ctx, path)
            if err is not None:
                last_error = err
                continue
            if len(ctx.get_ca_certs()) > 0:
                source = f"os_bundle:{path}"
                last_error = None
                break
            last_error = f"no CA certificates loaded from {path}"

    ca_count = len(ctx.get_ca_certs())
    ok = ca_count > 0
    if ok:
        last_error = None
    elif not last_error:
        last_error = "no CA certificates available in SSL context"

    info = {
        "ok": ok,
        "ca_count": ca_count,
        "source": source,
        "error": last_error,
    }
    return ctx, info


SSL_CONTEXT, SSL_CONTEXT_INFO = build_outbound_ssl_context()


# 必要な Twitch OAuth スコープ一覧（カテゴリ・タイトル変更 ＋ チャット ＋ フォロー ＋ チャネポ ＋ サブスク ＋ ビッツ）
REQUIRED_SCOPES = [
    "channel:manage:broadcast",
    "user:read:chat",
    "chat:read",
    "moderator:read:followers",
    "channel:read:redemptions",
    "channel:read:subscriptions",
    "channel:read:hype_train",
    "bits:read"
]

# カテゴリプリセット定義
# プリセット自体は公開側の Widget では使用しない個々の利用者向け設定のため、
# コードには組み込まず、twitch_config.json の category_presets で定義します
# （サーバー起動時に _apply_category_preset_overrides が読み込みます）。
# 形式: {"キー": {"game_id": "...", "game_name": "...", "display_name": "...",
#                "default_title": "..."}}
CATEGORY_PRESETS = {}

DEFAULT_COMMENT_VIEWER_SETTINGS = {
    "tts_enabled": True,
    "voicevox_url": "http://127.0.0.1:50021",
    "speaker_id": 3,          # 3: ずんだもん（ノーマル）
    "tts_speed": 1.05,
    "tts_volume": 1.0,
    "read_username": False,
    "read_alerts": True,
    "translate_enabled": True,
    "max_comments": 8,
    "hide_after_sec": 0       # 0 = 消さない（常時表示）、正数の場合は秒数後にフェードアウト
}


# load_twitch_config のキャッシュ (Issue #106 / 監査 P-02)。
# ファイルの stat (mtime_ns, サイズ) が変わらなければ読み直さない。
# save_twitch_config は os.replace でファイルを差し替えるため stat が変わり、
# 次回の読み込みで自動的に再読込される（明示的な無効化は不要）。
# 返り値は呼び出し側が書き換える前提（save の current.update() 等）のため、
# キャッシュ本体は返さず毎回ディープコピーを返す。
_CONFIG_CACHE = {"key": None, "value": None}


def _config_file_key():
    try:
        st = os.stat(CONFIG_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_twitch_config():
    with _CONFIG_LOCK:
        key = _config_file_key()
        cached = _CONFIG_CACHE["value"]
        if cached is not None and _CONFIG_CACHE["key"] == key:
            return copy.deepcopy(cached)
        fresh = _load_twitch_config_uncached()
        _CONFIG_CACHE["key"] = key
        _CONFIG_CACHE["value"] = fresh
        return copy.deepcopy(fresh)


def _apply_category_preset_overrides():
    """twitch_config.json の category_presets を CATEGORY_PRESETS へ読み込む。

    例: {"category_presets": {"chat": {"game_id": "509658", "game_name": "Just Chatting",
    "display_name": "雑談", "default_title": "自分の配信タイトル"}}}
    サーバー起動時に一度だけ適用されます。
    """
    try:
        overrides = load_twitch_config().get("category_presets")
    except Exception:
        return
    if not isinstance(overrides, dict):
        return
    for key, vals in overrides.items():
        if isinstance(vals, dict):
            CATEGORY_PRESETS.setdefault(key, {}).update(vals)




def _scrub_template_placeholders(cfg):
    """テンプレート (twitch_config.example.json) のプレースホルダ値を未設定として扱う (Issue #176)。

    テンプレートをコピーしただけの状態で残る "YOUR_..." は実値ではない。
    特に broadcaster_id が残ると、トークン保存時のチャンネル一致チェック
    (Issue #99) が「設定済みチャンネルと不一致」と判定し、初回 OAuth が
    403 で拒否される。読み込み時に空文字へ寄せ、未設定として扱う。
    broadcaster_id は Twitch のユーザー ID (数字のみ) でない値も未設定として扱う。
    """
    for key in _PROTECTED_NON_EMPTY_KEYS:
        value = cfg.get(key)
        if isinstance(value, str) and value.startswith("YOUR_"):
            cfg[key] = ""
    bid = cfg.get("broadcaster_id")
    if isinstance(bid, str) and bid and not (bid.isascii() and bid.isdigit()):
        cfg["broadcaster_id"] = ""


def _backup_corrupt_config_file():
    """読み込みに失敗した twitch_config.json を退避する (Issue #185 [6-2])。

    破損した状態で設定保存が走ると、デフォルト値に差分をマージした内容で
    ファイルが上書きされ、client_secret やトークンが失われる。元ファイルを
    `.corrupt` へ退避し、手動復旧の余地を残す。既存の退避と内容が異なる
    破損の場合は、時刻付きの名前で別に残す。
    """
    try:
        with open(CONFIG_FILE, "rb") as f:
            corrupt = f.read()
    except OSError:
        return
    if not corrupt:
        return
    backup = CONFIG_FILE + ".corrupt"
    try:
        if os.path.exists(backup):
            with open(backup, "rb") as f:
                if f.read() == corrupt:
                    return
            backup = CONFIG_FILE + ".corrupt-" + time.strftime("%Y%m%d%H%M%S")
        with open(backup, "wb") as f:
            f.write(corrupt)
        print(f"twitch_config.json を読み込めなかったため退避しました: {backup}")
    except OSError as e:
        print(f"Error backing up corrupt twitch_config.json: {e}")


def _load_twitch_config_uncached():
    with _CONFIG_LOCK:
        base = {
            "client_id": "",
            "client_secret": "",
            "access_token": "",
            "obs_password": "",
            "api_token": "",
            "current_ticker": "",
            "ticker_speed": 80,
            "refresh_token": "",
            "broadcaster_id": "",
            "channel_name": "",
            "youtube_handle": "",
            "youtube_video_id": "",
            "pngtuber_source_name": "",
            "viewer_settings": DEFAULT_COMMENT_VIEWER_SETTINGS.copy()
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        base.update(loaded)
                        if "viewer_settings" in loaded and isinstance(loaded["viewer_settings"], dict):
                            merged_vs = DEFAULT_COMMENT_VIEWER_SETTINGS.copy()
                            merged_vs.update(loaded["viewer_settings"])
                            base["viewer_settings"] = merged_vs
                        _scrub_template_placeholders(base)
                        return base
                    print("Error loading twitch_config.json: root must be a JSON object")
                    _backup_corrupt_config_file()
            except Exception as e:
                print(f"Error loading twitch_config.json: {e}")
                _backup_corrupt_config_file()
        return base


# HTTP API (/api/twitch/save-config) からクライアントが保存できる設定キーのホワイトリスト (Issue #49)。
# obs_ws_host / obs_ws_port / api_token 等は HTTP API 経由で変更させない。
CLIENT_WRITABLE_CONFIG_KEYS = frozenset({
    "client_id",
    "client_secret",
    "access_token",
    "refresh_token",
    "broadcaster_id",
    "channel_name",
    "obs_password",
    "current_ticker",
    "ticker_speed",
    "ticker_presets",
    "title_templates",
    "category_presets",
    "x_share_prefix",
    "youtube_handle",
    "youtube_video_id",
    "pngtuber_source_name",
    "viewer_settings",
})


def save_twitch_config(data, allow_empty_keys=(), allow_api_token=False, allowed_keys=None):
    if not isinstance(data, dict):
        return load_twitch_config()

    allowed_set = frozenset(allowed_keys) if allowed_keys is not None else None

    with _CONFIG_LOCK:
        current = load_twitch_config()
        sanitized = {}

        for k, v in data.items():
            # api_token はサーバー生成専用 (get_or_create_api_token) で、外部からの
            # 保存データに含まれていても書き込まない (Issue #174、多層防御)
            if k == "api_token" and not allow_api_token:
                continue
            if allowed_set is not None and k not in allowed_set:
                continue
            if k in _PROTECTED_NON_EMPTY_KEYS and k not in allow_empty_keys:
                if v is None or (isinstance(v, str) and v.strip() == ""):
                    if current.get(k):
                        continue
            sanitized[k] = v

        if "viewer_settings" in sanitized and isinstance(sanitized["viewer_settings"], dict):
            vs = current.get("viewer_settings", DEFAULT_COMMENT_VIEWER_SETTINGS.copy()).copy()
            vs.update(sanitized["viewer_settings"])
            sanitized["viewer_settings"] = vs

        current.update(sanitized)

        tmp_file = f"{CONFIG_FILE}.tmp.{threading.get_ident()}"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            if os.name == "posix":
                os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, CONFIG_FILE)
        except Exception as e:
            print(f"Error saving twitch_config.json: {e}")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
        return current


def get_or_create_api_token():
    """状態変更 API 用の共有トークンを返す。未設定なら生成して保存する (Issue #46)。

    トークンは Git 管理外の twitch_config.json にのみ保持し、HTTP では
    localhost 限定の発行エンドポイント以外で返さない。
    """
    with _CONFIG_LOCK:
        token = load_twitch_config().get("api_token", "")
        if token:
            return token
        token = secrets.token_urlsafe(24)
        save_twitch_config({"api_token": token}, allow_api_token=True)
        return token


ENV_LOCAL_JS_FILE = os.path.join(BASE_DIR, "shared", "env-config.local.js")
_ENV_LOCAL_PW_RE = re.compile(r"OBS_DEFAULT_PASSWORD\s*:\s*(['\"])((?:\\.|(?!\1).)*)\1")
_ENV_LOCAL_CACHE = {"key": None, "password": ""}


def read_env_local_obs_password(env_file=None, cache=None):
    """shared/env-config.local.js に書かれた OBS_DEFAULT_PASSWORD をフォールバック読み取りする。"""
    path = ENV_LOCAL_JS_FILE if env_file is None else env_file
    cache_dict = _ENV_LOCAL_CACHE if cache is None else cache
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    with _CONFIG_LOCK:
        if cache_dict.get("key") == key:
            return cache_dict.get("password", "")
    pw = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        m = _ENV_LOCAL_PW_RE.search(text)
        if m:
            raw_val = re.sub(r"\\([\\'\"])", r"\1", m.group(2))
            if raw_val and raw_val != "YOUR_OBS_PASSWORD":
                pw = raw_val
    except OSError:
        pw = ""
    with _CONFIG_LOCK:
        cache_dict["key"] = key
        cache_dict["password"] = pw
    return pw


# カテゴリプリセットの上書き適用（load_twitch_config 一式の定義後に実行）
_apply_category_preset_overrides()
