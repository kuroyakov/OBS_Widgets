"""
PNGTuber OBS WebSocket 音声レベル計測・設定管理モジュール (server/pngtuber_audio.py)

機能:
- pngtuber_config.json（無ければ pngtuber_config.example.json → 内蔵デフォルト）の読み込み
- OBS Studio 28+ 標準 obs-websocket (v5) の InputVolumeMeters イベントを購読し、
  設定された OBS 音声ソース（完全一致・部分一致、未指定時はマイク入力自動選択）の
  リアルタイム音量レベルを [0.0, 1.0] で保持する
- GET /api/pngtuber/stream (SSE) / GET /api/pngtuber/level 向けのレベル値・発話判定・接続状態返却
- 設定された4パターンのアバター画像パスの安全な解決（/api/pngtuber/image/<slot> 用）

設計方針:
- 外部音声ライブラリ（sounddevice）や外部 CLI（ffmpeg）には一切依存せず、
  標準ライブラリのみの obs-websocket クライアント (server/obs_ws.py) を利用する
- OBS 音声ミキサーのフィルタ（ノイズ抑制・ゲイン・ミュート）適用後レベルと一致する
- 音声データは録音・保存せず、InputVolumeMeters のレベル値のみを参照する
- SSE 購読・レベル取得ポーリングが 15 秒間途絶えたら WebSocket を閉じて一時停止し、
  高頻度 InputVolumeMeters 送信を止める（再アクセス時に自動再接続）
- OBS 未起動・未接続・対象ソース不在時は例外で落とさず device_connected=False を返し、
  2 秒周期で自動復帰する
"""

import copy
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse

from .config import (
    BASE_DIR,
    OBS_WS_UPSTREAM_HOST,
    OBS_WS_UPSTREAM_PORT,
    load_twitch_config,
)
from .obs_ws import (
    EVENT_SUBSCRIPTION_INPUTS,
    EVENT_SUBSCRIPTION_INPUT_VOLUME_METERS,
    ObsWebSocketClient,
    ObsWsError,
)

PNGTUBER_DIR = os.path.join(BASE_DIR, "PngTuber")
PNGTUBER_CONFIG_FILE = os.path.join(BASE_DIR, "pngtuber_config.json")
PNGTUBER_CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "pngtuber_config.example.json")
_ENV_LOCAL_JS_FILE = os.path.join(BASE_DIR, "shared", "env-config.local.js")

IMAGE_SLOTS = (
    "mouth_open_eye_open",
    "mouth_open_eye_closed",
    "mouth_closed_eye_open",
    "mouth_closed_eye_closed",
)

ALLOWED_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".apng": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".avif": "image/avif",
}

DEFAULT_PNGTUBER_CONFIG = {
    "images": {
        "mouth_open_eye_open": "images/mouth_open_eye_open.png",
        "mouth_open_eye_closed": "images/mouth_open_eye_closed.png",
        "mouth_closed_eye_open": "images/mouth_closed_eye_open.png",
        "mouth_closed_eye_closed": "images/mouth_closed_eye_closed.png",
    },
    "obs_input_name": "",
    "obs_ws_host": "",
    "obs_ws_port": None,
    "obs_ws_password": "",
    "open_threshold": 0.05,
    "close_threshold": 0.025,
    "dip_close_ratio": 0.82,
    "min_open_ms": 80,
    "min_closed_ms": 45,
    "blink_min_ms": 2200,
    "blink_max_ms": 5500,
    "poll_ms": 120,
}

# 自動選択時に音声ソースではない純粋な映像/テキスト種別を除外する
_NON_AUDIO_INPUT_KINDS = {
    "image_source",
    "color_source",
    "slideshow",
    "text_ft2_source",
    "text_gdiplus",
    "text_pango_source",
}

# OBS 音声種別のうち自動選択の候補に含めない出力/メディア系
_NON_MIC_AUDIO_KINDS = {
    "coreaudio_output_capture",
    "wasapi_output_capture",
    "wasapi_process_output_capture",
    "pulse_output_capture",
    "alsa_output_capture",
    "sck_audio_capture",
    "ffmpeg_source",
    "vlc_source",
    "browser_source",
}

_MIC_NAME_HINT_KEYWORDS = ("マイク", "mic", "microphone")

_ENV_LOCAL_PW_RE = re.compile(r"OBS_DEFAULT_PASSWORD\s*:\s*(['\"])((?:\\.|(?!\1).)*)\1")
_ENV_LOCAL_CACHE = {"key": None, "password": ""}

_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE = {"key": None, "value": None}



def _clamp_float(val, default, low, high):
    if isinstance(val, bool):
        return default
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return default
        return max(low, min(high, num))
    except (TypeError, ValueError):
        return default


def _clamp_int(val, default, low, high):
    if isinstance(val, bool):
        return default
    try:
        num = int(round(float(val)))
        return max(low, min(high, num))
    except (TypeError, ValueError):
        return default


def _normalize_optional_port(val):
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        port = int(val)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _normalize_single_avatar_config(raw, fallback=None):
    if not isinstance(raw, dict):
        raw = {}
    fb = fallback if isinstance(fallback, dict) else DEFAULT_PNGTUBER_CONFIG

    raw_images = raw.get("images")
    if not isinstance(raw_images, dict):
        raw_images = {}
    images = {}
    for slot in IMAGE_SLOTS:
        val = raw_images.get(slot)
        if val is None:
            val = fb["images"][slot]
        images[slot] = str(val).strip()

    # 新キー obs_input_name を優先し、旧キー mic_device_name も互換入力として受理する
    if "obs_input_name" in raw and raw["obs_input_name"] is not None:
        obs_input_name = str(raw["obs_input_name"]).strip()
    elif "mic_device_name" in raw and raw["mic_device_name"] is not None:
        obs_input_name = str(raw["mic_device_name"]).strip()
    else:
        obs_input_name = fb["obs_input_name"]

    obs_ws_host = str(raw.get("obs_ws_host") if raw.get("obs_ws_host") is not None else fb.get("obs_ws_host") or "").strip()
    obs_ws_port = _normalize_optional_port(raw.get("obs_ws_port") if "obs_ws_port" in raw else fb.get("obs_ws_port"))
    obs_ws_password = str(raw.get("obs_ws_password") if raw.get("obs_ws_password") is not None else fb.get("obs_ws_password") or "")

    open_threshold = _clamp_float(
        raw.get("open_threshold"),
        fb["open_threshold"],
        0.0001,
        1.0,
    )
    if "close_threshold" in raw and raw["close_threshold"] is not None:
        close_threshold = _clamp_float(
            raw.get("close_threshold"),
            min(open_threshold, fb["close_threshold"]),
            0.0,
            open_threshold,
        )
    else:
        close_threshold = min(open_threshold, fb["close_threshold"])
        if fallback is None and close_threshold > open_threshold:
            close_threshold = round(open_threshold * 0.6, 4)

    dip_close_ratio = _clamp_float(
        raw.get("dip_close_ratio"),
        fb["dip_close_ratio"],
        0.0,
        0.98,
    )
    min_open_ms = _clamp_int(
        raw.get("min_open_ms"),
        fb["min_open_ms"],
        0,
        1000,
    )
    min_closed_ms = _clamp_int(
        raw.get("min_closed_ms"),
        fb["min_closed_ms"],
        0,
        1000,
    )

    blink_min_ms = _clamp_int(
        raw.get("blink_min_ms"),
        fb["blink_min_ms"],
        200,
        60000,
    )
    blink_max_ms = _clamp_int(
        raw.get("blink_max_ms"),
        fb["blink_max_ms"],
        blink_min_ms,
        60000,
    )
    poll_ms = _clamp_int(
        raw.get("poll_ms"),
        fb["poll_ms"],
        50,
        2000,
    )

    return {
        "images": images,
        "obs_input_name": obs_input_name,
        "obs_ws_host": obs_ws_host,
        "obs_ws_port": obs_ws_port,
        "obs_ws_password": obs_ws_password,
        "open_threshold": open_threshold,
        "close_threshold": close_threshold,
        "dip_close_ratio": dip_close_ratio,
        "min_open_ms": min_open_ms,
        "min_closed_ms": min_closed_ms,
        "blink_min_ms": blink_min_ms,
        "blink_max_ms": blink_max_ms,
        "poll_ms": poll_ms,
    }


def _normalize_pngtuber_bundle(raw):
    if not isinstance(raw, dict):
        raw = {}
    default_cfg = _normalize_single_avatar_config(raw, DEFAULT_PNGTUBER_CONFIG)
    avatars = {}
    raw_avatars = raw.get("avatars")
    if isinstance(raw_avatars, dict):
        for k, v in raw_avatars.items():
            name = str(k or "").strip()
            if not name or name.lower() == "default" or not isinstance(v, dict):
                continue
            avatars[name] = _normalize_single_avatar_config(v, default_cfg)
    return {
        "default": default_cfg,
        "avatars": avatars,
    }


def _normalize_pngtuber_config(raw):
    return _normalize_pngtuber_bundle(raw)["default"]


def _resolve_config_path():
    if os.path.exists(PNGTUBER_CONFIG_FILE):
        return PNGTUBER_CONFIG_FILE
    if os.path.exists(PNGTUBER_CONFIG_EXAMPLE_FILE):
        return PNGTUBER_CONFIG_EXAMPLE_FILE
    return None


def _load_pngtuber_bundle():
    """PNGTuber 設定バンドル (default + avatars) を返す（stat キャッシュ付き、スレッドセーフ）。"""
    with _CONFIG_LOCK:
        path = _resolve_config_path()
        key = None
        if path is not None:
            try:
                st = os.stat(path)
                key = (path, st.st_mtime_ns, st.st_size)
            except OSError:
                key = (path, None, None)
        if _CONFIG_CACHE["value"] is not None and _CONFIG_CACHE["key"] == key:
            return copy.deepcopy(_CONFIG_CACHE["value"])
        raw = None
        if path is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                print(
                    f"[pngtuber] 設定ファイルの読み込みに失敗したためデフォルトで動作します: {path}: {e}",
                    file=sys.stderr,
                )
                raw = None
        normalized = _normalize_pngtuber_bundle(raw)
        _CONFIG_CACHE["key"] = key
        _CONFIG_CACHE["value"] = normalized
        return copy.deepcopy(normalized)


def resolve_avatar_name(avatar=None, bundle=None):
    """未指定・未知のアバター名はデフォルトアバター ('') として解決する。"""
    if bundle is None:
        bundle = _load_pngtuber_bundle()
    name = str(avatar or "").strip()
    if not name or name.lower() == "default":
        return ""
    if name in bundle.get("avatars", {}):
        return name
    return ""


def load_pngtuber_config(avatar=None):
    """PNGTuber 設定を返す（stat キャッシュ付き、スレッドセーフ）。"""
    bundle = _load_pngtuber_bundle()
    key = resolve_avatar_name(avatar, bundle)
    if key:
        return copy.deepcopy(bundle["avatars"][key])
    return copy.deepcopy(bundle["default"])


def _read_env_local_obs_password():
    """shared/env-config.local.js に書かれた OBS_DEFAULT_PASSWORD をフォールバック読み取りする。"""
    path = _ENV_LOCAL_JS_FILE
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    if _ENV_LOCAL_CACHE["key"] == key:
        return _ENV_LOCAL_CACHE["password"]
    pw = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        m = _ENV_LOCAL_PW_RE.search(text)
        if m:
            raw_val = m.group(2).replace(r"\\", "\\").replace(r"\'", "'").replace(r'\"', '"')
            if raw_val and raw_val != "YOUR_OBS_PASSWORD":
                pw = raw_val
    except OSError:
        pw = ""
    _ENV_LOCAL_CACHE["key"] = key
    _ENV_LOCAL_CACHE["password"] = pw
    return pw


def resolve_obs_ws_settings(png_cfg=None, twitch_cfg=None):
    """pngtuber_config.json > twitch_config.json > env-config.local.js / 既定値 の順で接続先を解決する。"""
    cfg = png_cfg if png_cfg is not None else load_pngtuber_config()
    t_cfg = twitch_cfg if twitch_cfg is not None else load_twitch_config()

    host = (
        str(cfg.get("obs_ws_host") or "").strip()
        or str(t_cfg.get("obs_ws_host") or "").strip()
        or OBS_WS_UPSTREAM_HOST
    )
    port = (
        _normalize_optional_port(cfg.get("obs_ws_port"))
        or _normalize_optional_port(t_cfg.get("obs_ws_port"))
        or OBS_WS_UPSTREAM_PORT
    )
    password = (
        str(cfg.get("obs_ws_password") or "")
        or str(t_cfg.get("obs_password") or "")
        or _read_env_local_obs_password()
    )
    return {"host": host, "port": port, "password": password}


def _strip_file_scheme(raw_path):
    if raw_path.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(raw_path)
        full_path = parsed.path or ""
        if parsed.fragment:
            full_path = f"{full_path}#{parsed.fragment}"
        return urllib.parse.unquote(full_path)
    return raw_path


def resolve_pngtuber_image_file(slot, avatar=None):
    """指定スロットの実ファイルパスと Content-Type を返す。解決不可時は (None, None)。"""
    if slot not in IMAGE_SLOTS:
        return None, None
    cfg = load_pngtuber_config(avatar=avatar)
    raw_path = (cfg["images"].get(slot) or "").strip()
    if not raw_path:
        return None, None
    lower = raw_path.lower()
    if lower.startswith(("http://", "https://", "data:")):
        return None, None

    cleaned = os.path.expanduser(_strip_file_scheme(raw_path))
    candidates = []
    if os.path.isabs(cleaned):
        candidates.append(cleaned)
        if cleaned.startswith("/PngTuber/"):
            candidates.append(os.path.join(BASE_DIR, cleaned.lstrip("/")))
    else:
        candidates.append(os.path.join(PNGTUBER_DIR, cleaned))
        candidates.append(os.path.join(BASE_DIR, cleaned))

    for candidate in candidates:
        try:
            real = os.path.realpath(candidate)
            if not os.path.isfile(real):
                continue
            ext = os.path.splitext(real)[1].lower()
            content_type = ALLOWED_IMAGE_EXTENSIONS.get(ext)
            if content_type:
                return real, content_type
        except OSError:
            continue
    return None, None


def _is_direct_pngtuber_relative(raw_path):
    if not raw_path or "://" in raw_path or raw_path.startswith(("/", "~", "data:")):
        return False
    norm = raw_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts or ".." in parts:
        return False
    # PngTuber/ 配下に存在するか、あるいはまだ未配置の相対パスの場合はそのまま相対URLとして扱う
    in_widget = os.path.isfile(os.path.join(PNGTUBER_DIR, *parts))
    in_repo = os.path.isfile(os.path.join(BASE_DIR, *parts))
    if in_repo and not in_widget:
        return False
    return True


def _image_label(raw_path, slot):
    if not raw_path:
        return ""
    lower = raw_path.lower()
    if lower.startswith("data:"):
        return f"data:{slot}"
    if lower.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw_path)
        return os.path.basename(parsed.path) or slot
    cleaned = _strip_file_scheme(raw_path)
    return os.path.basename(cleaned.rstrip("/\\")) or slot


def get_pngtuber_client_config(avatar=None):
    """フロントエンド (PngTuber/index.html) 向けにサニタイズ済み設定を返す。"""
    bundle = _load_pngtuber_bundle()
    resolved_avatar = resolve_avatar_name(avatar, bundle)
    cfg = bundle["avatars"][resolved_avatar] if resolved_avatar else bundle["default"]
    resolved_images = {}
    image_labels = {}
    for slot in IMAGE_SLOTS:
        raw_val = (cfg["images"].get(slot) or "").strip()
        image_labels[slot] = _image_label(raw_val, slot)
        if not raw_val:
            resolved_images[slot] = ""
            continue
        lower = raw_val.lower()
        if lower.startswith(("http://", "https://", "data:")):
            resolved_images[slot] = raw_val
            continue
        if _is_direct_pngtuber_relative(raw_val):
            rel_url = urllib.parse.quote(raw_val.replace("\\", "/"), safe="/:@-._~!$&'()*+,;=%")
            filepath, _ = resolve_pngtuber_image_file(slot, avatar=resolved_avatar)
            if filepath:
                try:
                    mtime_ns = os.stat(filepath).st_mtime_ns
                    resolved_images[slot] = f"{rel_url}?v={mtime_ns}"
                    continue
                except OSError:
                    pass
            resolved_images[slot] = rel_url
            continue
        filepath, _ = resolve_pngtuber_image_file(slot, avatar=resolved_avatar)
        params = []
        if filepath:
            try:
                params.append(f"v={os.stat(filepath).st_mtime_ns}")
            except OSError:
                pass
        if resolved_avatar:
            params.append(f"avatar={urllib.parse.quote(resolved_avatar)}")
        qs = f"?{'&'.join(params)}" if params else ""
        resolved_images[slot] = f"/api/pngtuber/image/{slot}{qs}"

    return {
        "avatar": resolved_avatar or "default",
        "images": resolved_images,
        "image_labels": image_labels,
        "obs_input_name": cfg["obs_input_name"],
        "matched_input_name": PNGTUBER_MONITOR.get_status(avatar=resolved_avatar).get("device_name", ""),
        "open_threshold": cfg["open_threshold"],
        "close_threshold": cfg["close_threshold"],
        "dip_close_ratio": cfg["dip_close_ratio"],
        "min_open_ms": cfg["min_open_ms"],
        "min_closed_ms": cfg["min_closed_ms"],
        "blink_min_ms": cfg["blink_min_ms"],
        "blink_max_ms": cfg["blink_max_ms"],
        "poll_ms": cfg["poll_ms"],
        "audio_backend": PNGTUBER_MONITOR.resolved_backend_name(),
    }


def _input_audio_tier(item):
    """OBS 入力種別のマイク優先度 (0: マイク系, 1: その他音声/メディア, 2: 種別未設定/映像キャプチャ, None: 純粋映像)。"""
    kind = str(item.get("unversionedInputKind") or item.get("inputKind") or "").strip().lower()
    if kind in _NON_AUDIO_INPUT_KINDS:
        return None
    if "input_capture" in kind or "mic" in kind:
        return 0
    if kind in _NON_MIC_AUDIO_KINDS or "audio" in kind or "jack" in kind or "pipewire" in kind:
        return 1
    return 2


def match_obs_input(inputs, query=""):
    """OBS GetInputList または InputVolumeMeters の inputs 配列から対象ソースを選ぶ。

    - query が空または default/auto のとき: OBS マイク入力種別 (*_input_capture) を優先選択
      （種別情報がない場合はマイク名キーワード、それも無ければ先頭要素）
    - query が非空のとき: 大文字小文字同等の完全一致 > 大文字小文字無視完全一致 > 大文字小文字無視部分一致
      の順に、同ランク内では音声系ソースを優先して返す
    """
    if not isinstance(inputs, list) or not inputs:
        return None

    q = (query or "").strip()
    q_lower = q.lower()
    has_kind_info = any(
        isinstance(item, dict) and (item.get("inputKind") or item.get("unversionedInputKind"))
        for item in inputs
    )

    if not q or q_lower in ("default", "auto"):
        for tier_target in (0,):
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("inputName") or "")
                if not name:
                    continue
                if _input_audio_tier(item) == tier_target:
                    return item

        for item in inputs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("inputName") or "")
            kind = str(item.get("unversionedInputKind") or item.get("inputKind") or "").strip().lower()
            tier = _input_audio_tier(item)
            if not name or tier is None or kind in _NON_MIC_AUDIO_KINDS:
                continue
            name_lower = name.lower()
            if any(kw in name_lower for kw in _MIC_NAME_HINT_KEYWORDS):
                return item

        if not has_kind_info:
            for item in inputs:
                if isinstance(item, dict) and item.get("inputName"):
                    return item
        return None

    best = None
    best_score = None
    for idx, item in enumerate(inputs):
        if not isinstance(item, dict):
            continue
        name = str(item.get("inputName") or "")
        if not name:
            continue
        tier = _input_audio_tier(item)
        if tier is None:
            continue
        name_lower = name.lower()
        if name == q:
            match_rank = 0
        elif name_lower == q_lower:
            match_rank = 1
        elif q_lower in name_lower:
            match_rank = 2
        else:
            continue
        score = (match_rank, tier, idx)
        if best_score is None or score < best_score:
            best_score = score
            best = item
    return best


def extract_channel_level(channel_levels):
    """OBS InputVolumeMeters の 1 チャンネル配列 [magnitude, peak, inputPeak] から [0.0, 1.0] を取り出す。"""
    if not isinstance(channel_levels, (list, tuple)) or not channel_levels:
        return 0.0
    # ch[0] = magnitude（OBS ミキサーの平滑 RMS 相当・無音時の立ち下がりが自然で口パク向き）
    raw = channel_levels[0]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    val = float(raw)
    if not math.isfinite(val):
        return 0.0
    return max(0.0, min(1.0, val))


def extract_input_level_from_volume_meters(event_data, target_query="", resolved_input_name=None):
    """OBS InputVolumeMeters の eventData から対象ソース名と音量レベル (0.0〜1.0) を抽出する。

    戻り値: (matched_input_name_or_None, level_float)
    """
    if not isinstance(event_data, dict):
        return None, 0.0
    inputs = event_data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return None, 0.0

    target_item = None
    if resolved_input_name:
        for item in inputs:
            if isinstance(item, dict) and item.get("inputName") == resolved_input_name:
                target_item = item
                break
    else:
        target_item = match_obs_input(inputs, target_query)

    if target_item is None:
        return None, 0.0

    levels_mul = target_item.get("inputLevelsMul")
    peak = 0.0
    if isinstance(levels_mul, list):
        for ch in levels_mul:
            lvl = extract_channel_level(ch)
            if lvl > peak:
                peak = lvl
    return str(target_item.get("inputName") or ""), peak


def extract_all_input_levels_from_volume_meters(event_data):
    """OBS InputVolumeMeters の eventData から {inputName: level_float} を抽出する。"""
    if not isinstance(event_data, dict):
        return {}
    inputs = event_data.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return {}

    levels = {}
    for item in inputs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("inputName") or "")
        if not name:
            continue
        levels_mul = item.get("inputLevelsMul")
        peak = 0.0
        if isinstance(levels_mul, list):
            for ch in levels_mul:
                lvl = extract_channel_level(ch)
                if lvl > peak:
                    peak = lvl
        levels[name] = peak
    return levels


def _make_avatar_state():
    return {
        "level": 0.0,
        "speaking": False,
        "device_connected": False,
        "device_name": "",
        "is_muted": False,
        "event_seq": 0,
        "active_streams": 0,
        "last_poll_ts": None,
    }


class PngTuberAudioMonitor:
    """OBS WebSocket (v5) 経由で OBS 音声ミキサーの InputVolumeMeters を監視するモニター。"""

    IDLE_TIMEOUT_SEC = 15.0
    RECONNECT_INTERVAL_SEC = 2.0
    POLL_SLICE_SEC = 0.5
    CONFIG_CHECK_INTERVAL_SEC = 0.25
    MAX_STREAMS = 12

    def __init__(
        self,
        config_loader=load_pngtuber_config,
        ws_settings_loader=resolve_obs_ws_settings,
        client_factory=ObsWebSocketClient,
    ):
        self._config_loader = config_loader
        self._ws_settings_loader = ws_settings_loader
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._probe_event = threading.Event()
        self._thread = None
        self._active_client = None

        self._avatar_states = {"": _make_avatar_state()}
        self._obs_inputs = None
        self._input_mutes = {}
        self._backend_name = "obs-websocket"
        self._last_error = ""
        self._active_queries = None
        self._bundle_dirty = False
        self._bundle_gen = 0

    def _get_bundle(self):
        if self._config_loader is load_pngtuber_config:
            return _load_pngtuber_bundle()
        raw = self._config_loader()
        if isinstance(raw, dict) and "default" in raw and "avatars" in raw:
            return raw
        return _normalize_pngtuber_bundle(raw)

    def _get_effective_bundle(self):
        """現在の設定バンドルを返す。

        設定ファイルの変更は stat キャッシュで自動検出され、監視ループが
        定期チェックで対象マイクの再解決を行う。実行時の割当変更は、
        外部ツールが設定ファイル自体を書き換えることで行う。
        """
        return self._get_bundle()

    @staticmethod
    def _bundle_avatar_keys(bundle):
        return [""] + list(bundle.get("avatars", {}).keys())

    @staticmethod
    def _queries_snapshot(bundle):
        snap = {"": bundle["default"]["obs_input_name"]}
        for k, v in bundle.get("avatars", {}).items():
            snap[k] = v["obs_input_name"]
        return snap

    def _get_avatar_state_locked(self, av_key):
        st = self._avatar_states.get(av_key)
        if st is None:
            st = _make_avatar_state()
            self._avatar_states[av_key] = st
        return st

    def _ensure_started(self):
        with self._start_lock:
            if self._stop_event.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._probe_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="pngtuber-obs-ws-monitor",
                daemon=True,
            )
            self._thread.start()

    def resolved_backend_name(self):
        with self._lock:
            return self._backend_name

    def is_stopped(self):
        return self._stop_event.is_set()

    def get_level(self, avatar=None):
        """現在の OBS 音声レベル・発話判定・接続状態を返す（読み取り用）。"""
        bundle = self._get_bundle()
        av_key = resolve_avatar_name(avatar, bundle)
        now = time.monotonic()
        with self._cond:
            was_idle = self._is_idle_locked(now)
            st = self._get_avatar_state_locked(av_key)
            st["last_poll_ts"] = now
            if was_idle:
                self._probe_event.clear()
                self._cond.notify_all()
        self._ensure_started()
        if not self._probe_event.is_set():
            self._probe_event.wait(timeout=0.8)
        with self._lock:
            st = self._get_avatar_state_locked(av_key)
            return {
                "level": round(st["level"], 4),
                "speaking": st["speaking"],
                "device_connected": st["device_connected"],
            }

    def register_stream(self, avatar=None):
        """SSE ストリーム購読を開始し、監視スレッドを必要なら起動する。上限超過・終了時は False。"""
        bundle = self._get_bundle()
        av_key = resolve_avatar_name(avatar, bundle)
        now = time.monotonic()
        with self._cond:
            total_streams = sum(s["active_streams"] for s in self._avatar_states.values())
            if self._stop_event.is_set() or total_streams >= self.MAX_STREAMS:
                return False
            was_idle = self._is_idle_locked(now)
            st = self._get_avatar_state_locked(av_key)
            st["active_streams"] += 1
            st["last_poll_ts"] = now
            if was_idle:
                self._probe_event.clear()
            self._cond.notify_all()
        self._ensure_started()
        return True

    def unregister_stream(self, avatar=None):
        """SSE ストリーム購読を終了する。最後の購読者が抜けると一定時間後に WS が自動停止する。"""
        bundle = self._get_bundle()
        av_key = resolve_avatar_name(avatar, bundle)
        with self._cond:
            st = self._get_avatar_state_locked(av_key)
            if st["active_streams"] > 0:
                st["active_streams"] -= 1
            st["last_poll_ts"] = time.monotonic()
            self._cond.notify_all()

    def wait_stream_update(self, last_seq=None, timeout=2.0, avatar=None):
        """新しいレベルまたは接続状態変化が来るまで待機し (changed, seq, payload_or_None) を返す。"""
        bundle = self._get_bundle()
        av_key = resolve_avatar_name(avatar, bundle)
        deadline = time.monotonic() + max(0.05, float(timeout))
        with self._cond:
            while not self._stop_event.is_set():
                now = time.monotonic()
                st = self._get_avatar_state_locked(av_key)
                st["last_poll_ts"] = now
                if last_seq is None or st["event_seq"] != last_seq:
                    return True, st["event_seq"], {
                        "level": round(st["level"], 4),
                        "speaking": st["speaking"],
                        "device_connected": st["device_connected"],
                    }
                remaining = deadline - now
                if remaining <= 0:
                    return False, st["event_seq"], None
                self._cond.wait(timeout=remaining)
            st = self._get_avatar_state_locked(av_key)
            return False, st["event_seq"], None

    def get_status(self, avatar=None):
        """監視を強制起動せずに現在の内部スナップショットを返す。"""
        bundle = self._get_bundle()
        av_key = resolve_avatar_name(avatar, bundle)
        with self._lock:
            st = self._get_avatar_state_locked(av_key)
            return {
                "level": round(st["level"], 4),
                "speaking": st["speaking"],
                "device_connected": st["device_connected"],
                "device_name": st["device_name"],
                "backend": self._backend_name,
                "last_error": self._last_error,
            }

    def stop(self):
        """監視スレッドと OBS WebSocket 接続を安全に終了する。"""
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        self._close_active_client()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    def _close_active_client(self):
        with self._lock:
            client = self._active_client
            self._active_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _set_disconnected_all(self, error_msg="", backend_name="obs-websocket"):
        with self._cond:
            any_changed = False
            for st in self._avatar_states.values():
                changed = (
                    st["level"] != 0.0
                    or st["speaking"]
                    or st["device_connected"]
                    or bool(st["device_name"])
                    or st["is_muted"]
                )
                st["level"] = 0.0
                st["speaking"] = False
                st["device_connected"] = False
                st["device_name"] = ""
                st["is_muted"] = False
                if changed:
                    st["event_seq"] += 1
                    any_changed = True
            self._obs_inputs = None
            self._input_mutes.clear()
            self._active_queries = None
            self._bundle_dirty = False
            if backend_name is not None:
                self._backend_name = backend_name
            if error_msg is not None:
                self._last_error = error_msg
            if any_changed:
                self._cond.notify_all()
        self._probe_event.set()

    def _update_all_levels(self, levels_by_name, bundle):
        with self._cond:
            any_changed = False
            for av_key in self._bundle_avatar_keys(bundle):
                av_cfg = bundle["avatars"][av_key] if av_key else bundle["default"]
                open_th = av_cfg["open_threshold"]
                close_th = av_cfg["close_threshold"]
                st = self._get_avatar_state_locked(av_key)
                if not st["device_connected"] or not st["device_name"]:
                    continue
                if st["is_muted"]:
                    if st["level"] != 0.0 or st["speaking"]:
                        st["level"] = 0.0
                        st["speaking"] = False
                        st["event_seq"] += 1
                        any_changed = True
                    continue
                raw_level = levels_by_name.get(st["device_name"], 0.0)
                raw_clamped = max(0.0, min(1.0, float(raw_level)))
                prev_level = st["level"]
                prev_speaking = st["speaking"]
                # OBS 側 RMS (magnitude) を減衰スマアリング無しでそのまま反映し、音節の谷とアタックを即時に伝える
                st["level"] = raw_clamped
                if st["speaking"]:
                    if st["level"] <= close_th:
                        st["speaking"] = False
                else:
                    if st["level"] >= open_th:
                        st["speaking"] = True
                if raw_clamped > 0.0 or prev_level > 0.0 or st["speaking"] != prev_speaking:
                    st["event_seq"] += 1
                    any_changed = True
            if any_changed:
                self._cond.notify_all()

    def _zero_all_active_levels(self):
        with self._cond:
            any_changed = False
            for st in self._avatar_states.values():
                if st["device_connected"] and not st["is_muted"] and (st["level"] > 0.0 or st["speaking"]):
                    st["level"] = 0.0
                    st["speaking"] = False
                    st["event_seq"] += 1
                    any_changed = True
            if any_changed:
                self._cond.notify_all()

    def _is_idle_locked(self, now=None):
        if now is None:
            now = time.monotonic()
        for st in self._avatar_states.values():
            if st["active_streams"] > 0:
                return False
            if st["last_poll_ts"] is not None and (now - st["last_poll_ts"]) <= self.IDLE_TIMEOUT_SEC:
                return False
        return True

    def _is_idle(self):
        with self._lock:
            return self._is_idle_locked()

    def _should_stop_stream(self, active_ws_key, check_config=True):
        if self._stop_event.is_set():
            return True
        if self._is_idle():
            return True
        if check_config:
            bundle = self._get_bundle()
            ws_cfg = self._ws_settings_loader(bundle["default"])
            ws_key = (ws_cfg["host"], ws_cfg["port"], ws_cfg["password"])
            if ws_key != active_ws_key:
                return True
        return False

    def _resolve_all_targets(self, client, bundle, resolved_gen=None):
        """OBS から入力一覧・ミュート状態を取得し、全アバターの対象ソースを解決する。"""
        data = client.request("GetInputList")
        inputs = data.get("inputs", []) if isinstance(data, dict) else []
        resolved_map = {}
        mute_cache = {}
        first_error = ""

        for av_key in self._bundle_avatar_keys(bundle):
            av_cfg = bundle["avatars"][av_key] if av_key else bundle["default"]
            query = av_cfg["obs_input_name"]
            matched = match_obs_input(inputs, query)
            if not matched:
                resolved_map[av_key] = (None, False)
                if not first_error:
                    first_error = f"OBS audio input not found: {query or 'auto'}"
                continue
            resolved_name = str(matched.get("inputName") or "")
            if resolved_name not in mute_cache:
                try:
                    mute_data = client.request("GetInputMute", {"inputName": resolved_name})
                    mute_cache[resolved_name] = bool(mute_data.get("inputMuted", False))
                except ObsWsError:
                    mute_cache[resolved_name] = False
            resolved_map[av_key] = (resolved_name, mute_cache[resolved_name])

        has_unresolved = False
        with self._cond:
            self._obs_inputs = inputs
            self._input_mutes.update(mute_cache)
            self._backend_name = "obs-websocket"
            self._last_error = first_error
            self._active_queries = self._queries_snapshot(bundle)
            if resolved_gen is None or self._bundle_gen == resolved_gen:
                self._bundle_dirty = False
            any_changed = False
            for av_key, (resolved_name, is_muted) in resolved_map.items():
                st = self._get_avatar_state_locked(av_key)
                if resolved_name is None:
                    has_unresolved = True
                    changed = (
                        st["level"] != 0.0
                        or st["speaking"]
                        or st["device_connected"]
                        or bool(st["device_name"])
                        or st["is_muted"]
                    )
                    st["level"] = 0.0
                    st["speaking"] = False
                    st["device_connected"] = False
                    st["device_name"] = ""
                    st["is_muted"] = False
                    if changed:
                        st["event_seq"] += 1
                        any_changed = True
                else:
                    target_switched = bool(st["device_name"]) and (st["device_name"] != resolved_name)
                    changed = (
                        (not st["device_connected"])
                        or (st["device_name"] != resolved_name)
                        or (st["is_muted"] != is_muted)
                    )
                    st["device_connected"] = True
                    st["device_name"] = resolved_name
                    st["is_muted"] = is_muted
                    if target_switched or (is_muted and (st["level"] != 0.0 or st["speaking"])):
                        if st["level"] != 0.0 or st["speaking"]:
                            changed = True
                        st["level"] = 0.0
                        st["speaking"] = False
                    if changed:
                        st["event_seq"] += 1
                        any_changed = True
            if any_changed:
                self._cond.notify_all()
        self._probe_event.set()
        return has_unresolved

    def _run_loop(self):
        while not self._stop_event.is_set():
            if self._is_idle():
                self._set_disconnected_all("", "obs-websocket")
                with self._cond:
                    while not self._stop_event.is_set() and self._is_idle_locked():
                        self._cond.wait(timeout=1.0)
                continue

            bundle = self._get_bundle()
            ws_cfg = self._ws_settings_loader(bundle["default"])
            self._run_obs_ws_session(ws_cfg)

            if self._stop_event.is_set():
                break
            if not self._is_idle():
                # 切断時・未接続時の再試行待ち（終了要求があれば即起床）
                self._stop_event.wait(timeout=self.RECONNECT_INTERVAL_SEC)

    def _run_obs_ws_session(self, ws_cfg):
        ws_key = (ws_cfg["host"], ws_cfg["port"], ws_cfg["password"])
        subs = EVENT_SUBSCRIPTION_INPUTS | EVENT_SUBSCRIPTION_INPUT_VOLUME_METERS
        client = self._client_factory(
            host=ws_cfg["host"],
            port=ws_cfg["port"],
            password=ws_cfg["password"],
            timeout=2.0,
            event_subscriptions=subs,
        )
        with self._lock:
            self._active_client = client

        session_error = ""
        try:
            client.connect()
            if self._should_stop_stream(ws_key):
                return

            with self._lock:
                mic_gen = self._bundle_gen
            bundle = self._get_effective_bundle()
            active_queries = self._queries_snapshot(bundle)
            has_unresolved = self._resolve_all_targets(client, bundle, resolved_gen=mic_gen)
            last_recheck_ts = time.monotonic()
            last_cfg_check_ts = last_recheck_ts

            while True:
                now = time.monotonic()
                with self._lock:
                    mic_dirty = self._bundle_dirty
                    mic_gen = self._bundle_gen
                do_cfg_check = mic_dirty or (now - last_cfg_check_ts) >= self.CONFIG_CHECK_INTERVAL_SEC
                if do_cfg_check:
                    last_cfg_check_ts = now
                if self._should_stop_stream(ws_key, check_config=do_cfg_check):
                    break

                if do_cfg_check:
                    bundle = self._get_effective_bundle()
                    new_queries = self._queries_snapshot(bundle)
                    if mic_dirty or new_queries != active_queries:
                        active_queries = new_queries
                        has_unresolved = self._resolve_all_targets(client, bundle, resolved_gen=mic_gen)
                        last_recheck_ts = now

                if has_unresolved and (now - last_recheck_ts) >= self.RECONNECT_INTERVAL_SEC:
                    last_recheck_ts = now
                    with self._lock:
                        mic_gen = self._bundle_gen
                    bundle = self._get_effective_bundle()
                    has_unresolved = self._resolve_all_targets(client, bundle, resolved_gen=mic_gen)

                msg = client.poll_message(timeout=self.POLL_SLICE_SEC)
                if msg is None:
                    self._zero_all_active_levels()
                    continue

                if msg.get("op") != 5:
                    continue
                d = msg.get("d") or {}
                event_type = d.get("eventType")
                event_data = d.get("eventData") or {}

                if event_type == "InputVolumeMeters":
                    with self._lock:
                        mic_dirty = self._bundle_dirty
                        mic_gen = self._bundle_gen
                    bundle = self._get_effective_bundle()
                    if mic_dirty:
                        active_queries = self._queries_snapshot(bundle)
                        has_unresolved = self._resolve_all_targets(client, bundle, resolved_gen=mic_gen)
                        last_recheck_ts = time.monotonic()
                    levels_by_name = extract_all_input_levels_from_volume_meters(event_data)
                    self._update_all_levels(levels_by_name, bundle)
                elif event_type == "InputMuteStateChanged":
                    muted_name = str(event_data.get("inputName") or "")
                    if muted_name:
                        muted_val = bool(event_data.get("inputMuted", False))
                        with self._cond:
                            self._input_mutes[muted_name] = muted_val
                            any_changed = False
                            for st in self._avatar_states.values():
                                if st["device_name"] == muted_name:
                                    st["is_muted"] = muted_val
                                    if muted_val and (st["level"] != 0.0 or st["speaking"]):
                                        st["level"] = 0.0
                                        st["speaking"] = False
                                        st["event_seq"] += 1
                                        any_changed = True
                            if any_changed:
                                self._cond.notify_all()
                elif event_type in ("InputCreated", "InputRemoved", "InputNameChanged"):
                    with self._lock:
                        mic_gen = self._bundle_gen
                    bundle = self._get_effective_bundle()
                    active_queries = self._queries_snapshot(bundle)
                    has_unresolved = self._resolve_all_targets(client, bundle, resolved_gen=mic_gen)
                    last_recheck_ts = time.monotonic()
        except Exception as e:
            session_error = str(e)[:200]
        finally:
            with self._lock:
                if self._active_client is client:
                    self._active_client = None
            try:
                client.close()
            except Exception:
                pass
            self._set_disconnected_all(session_error, "obs-websocket")


PNGTUBER_MONITOR = PngTuberAudioMonitor()


def get_pngtuber_level(avatar=None):
    return PNGTUBER_MONITOR.get_level(avatar=avatar)


def register_pngtuber_stream(avatar=None):
    return PNGTUBER_MONITOR.register_stream(avatar=avatar)


def unregister_pngtuber_stream(avatar=None):
    PNGTUBER_MONITOR.unregister_stream(avatar=avatar)


def wait_pngtuber_stream_update(last_seq=None, timeout=2.0, avatar=None):
    return PNGTUBER_MONITOR.wait_stream_update(last_seq=last_seq, timeout=timeout, avatar=avatar)


def is_pngtuber_stopped():
    return PNGTUBER_MONITOR.is_stopped()


def stop_pngtuber_audio():
    PNGTUBER_MONITOR.stop()
