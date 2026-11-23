"""
音声認識字幕ちゃん連動 OBS 音声コマンドモジュール (server/voice_command.py)

機能:
- voice_command_config.json のホットリロード ((mtime_ns, size) 監視)・原子的保存・破損退避
- ウェイクワード検出と最長一致優先のコマンドマッチング
- OBS WebSocket 経由での音量調整 (dB 上下限ガード)・ミュート・ソースフィルタ切替・
  ホットキー実行・離席中モード (Ticker 退避/復帰)
- 実行結果の BroadcastCustomEvent (realm: obs-voice-command-v1) 送出および実行履歴保持
"""

from collections import deque
import copy
import json
import math
import os
import re
import threading
import time

from server.config import BASE_DIR, load_twitch_config, save_twitch_config
from server.obs_ws import ObsWebSocketClient, ObsWsError

VOICE_COMMAND_CONFIG_FILE = os.path.join(BASE_DIR, "voice_command_config.json")

DEFAULT_AWAY_MESSAGE = "離席中です / Away from keyboard"

DEFAULT_VOICE_COMMAND_CONFIG = {
    "enabled": True,
    "wake_words": ["コマンド"],
    "cooldown_sec": 1.5,
    "volume_step_db": 2.0,
    "volume_min_db": -60.0,
    "volume_max_db": 0.0,
    "audio_targets": [],
    "custom_commands": [],
    "away_message": "",
    "normal_message": "",
}

_AUDIO_OPS = (
    ("ミュート解除", "unmute"),
    ("みゅーとかいじょ", "unmute"),
    ("ミュートオフ", "unmute"),
    ("ミュート", "mute"),
    ("みゅーと", "mute"),
    ("消音", "mute"),
    ("上げて", "volume_up"),
    ("あげて", "volume_up"),
    ("大きく", "volume_up"),
    ("おおきく", "volume_up"),
    ("アップ", "volume_up"),
    ("あっぷ", "volume_up"),
    ("下げて", "volume_down"),
    ("さげて", "volume_down"),
    ("小さく", "volume_down"),
    ("ちいさく", "volume_down"),
    ("ダウン", "volume_down"),
    ("だうん", "volume_down"),
)

_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE = {"key": None, "value": None}

_EXEC_LOCK = threading.Lock()
_LAST_EXEC_TS = None
_SAVED_TICKER_BEFORE_AWAY = None
_HISTORY = deque(maxlen=50)


def _compact(text):
    return re.sub(r"[\s、。,.!！?？:：]+", "", str(text or "")).lower()


def _config_file_key(path=None):
    target = path or VOICE_COMMAND_CONFIG_FILE
    try:
        st = os.stat(target)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _backup_corrupt_config_file(path):
    try:
        with open(path, "rb") as f:
            corrupt = f.read()
    except OSError:
        return
    if not corrupt:
        return
    backup = path + ".corrupt"
    try:
        if os.path.exists(backup):
            with open(backup, "rb") as f:
                if f.read() == corrupt:
                    return
            backup = path + ".corrupt-" + time.strftime("%Y%m%d%H%M%S")
        with open(backup, "wb") as f:
            f.write(corrupt)
        print(f"[voice_command] 破損した設定ファイルを退避しました: {backup}")
    except OSError as e:
        print(f"[voice_command] 設定ファイルの退避に失敗しました: {e}")


def _normalize_config(raw):
    base = copy.deepcopy(DEFAULT_VOICE_COMMAND_CONFIG)
    if not isinstance(raw, dict):
        return base
    if isinstance(raw.get("enabled"), bool):
        base["enabled"] = raw["enabled"]
    if isinstance(raw.get("wake_words"), list):
        words = [str(w).strip() for w in raw["wake_words"] if isinstance(w, str) and w.strip()]
        if words:
            base["wake_words"] = words
    for num_key, min_val in (("cooldown_sec", 0.0), ("volume_step_db", 0.1)):
        v = raw.get(num_key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= min_val:
            base[num_key] = float(v)
    vmin = raw.get("volume_min_db")
    vmax = raw.get("volume_max_db")
    if (
        isinstance(vmin, (int, float)) and not isinstance(vmin, bool) and math.isfinite(vmin)
        and isinstance(vmax, (int, float)) and not isinstance(vmax, bool) and math.isfinite(vmax)
        and vmin <= vmax
    ):
        base["volume_min_db"] = float(vmin)
        base["volume_max_db"] = float(vmax)
    if isinstance(raw.get("audio_targets"), list):
        targets = []
        for item in raw["audio_targets"]:
            if not isinstance(item, dict):
                continue
            input_name = str(item.get("input_name") or "").strip()
            aliases = [str(a).strip() for a in (item.get("aliases") or []) if isinstance(a, str) and a.strip()]
            if input_name and aliases:
                targets.append({"aliases": aliases, "input_name": input_name})
        base["audio_targets"] = targets
    if isinstance(raw.get("custom_commands"), list):
        cmds = []
        for item in raw["custom_commands"]:
            if not isinstance(item, dict):
                continue
            ctype = str(item.get("type") or "").strip()
            aliases = [str(a).strip() for a in (item.get("aliases") or []) if isinstance(a, str) and a.strip()]
            if not aliases or ctype not in ("filter", "hotkey", "away"):
                continue
            if ctype == "filter":
                src = str(item.get("source_name") or "").strip()
                flt = str(item.get("filter_name") or "").strip()
                if not src or not flt:
                    continue
                disable_filters = [
                    str(df).strip() for df in (item.get("disable_filters") or [])
                    if isinstance(df, str) and df.strip()
                ]
                cmds.append({
                    "aliases": aliases,
                    "type": "filter",
                    "source_name": src,
                    "filter_name": flt,
                    "enabled": bool(item.get("enabled", True)),
                    "disable_filters": disable_filters,
                })
            elif ctype == "hotkey":
                hk = str(item.get("hotkey_name") or "").strip()
                if not hk:
                    continue
                cmds.append({
                    "aliases": aliases,
                    "type": "hotkey",
                    "hotkey_name": hk,
                })
            elif ctype == "away":
                mode = str(item.get("mode") or "on").strip().lower()
                if mode not in ("on", "off"):
                    continue
                cmds.append({
                    "aliases": aliases,
                    "type": "away",
                    "mode": mode,
                })
        base["custom_commands"] = cmds
    if isinstance(raw.get("away_message"), str):
        base["away_message"] = raw["away_message"]
    if isinstance(raw.get("normal_message"), str):
        base["normal_message"] = raw["normal_message"]
    return base


def _load_uncached(path):
    if not os.path.exists(path):
        return copy.deepcopy(DEFAULT_VOICE_COMMAND_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("root must be a JSON object")
        return _normalize_config(loaded)
    except (OSError, ValueError) as e:
        print(f"[voice_command] 設定ファイルの読み込みに失敗しました ({e})")
        _backup_corrupt_config_file(path)
        return copy.deepcopy(DEFAULT_VOICE_COMMAND_CONFIG)


def load_voice_command_config(config_path=None):
    target = config_path or VOICE_COMMAND_CONFIG_FILE
    with _CONFIG_LOCK:
        if target == VOICE_COMMAND_CONFIG_FILE:
            key = _config_file_key(target)
            cached = _CONFIG_CACHE["value"]
            if cached is not None and _CONFIG_CACHE["key"] == key:
                return copy.deepcopy(cached)
            fresh = _load_uncached(target)
            _CONFIG_CACHE["key"] = key
            _CONFIG_CACHE["value"] = fresh
            return copy.deepcopy(fresh)
        return _load_uncached(target)


def save_voice_command_config(updates, config_path=None):
    if not isinstance(updates, dict):
        raise ValueError("updates must be a JSON object")
    target = config_path or VOICE_COMMAND_CONFIG_FILE
    with _CONFIG_LOCK:
        current = load_voice_command_config(config_path=target)
        merged = copy.deepcopy(current)

        if "enabled" in updates:
            if not isinstance(updates["enabled"], bool):
                raise ValueError("enabled must be a boolean")
            merged["enabled"] = updates["enabled"]
        if "wake_words" in updates:
            if not isinstance(updates["wake_words"], list):
                raise ValueError("wake_words must be a list")
            words = [str(w).strip() for w in updates["wake_words"] if isinstance(w, str) and w.strip()]
            if not words:
                raise ValueError("wake_words must contain at least one non-empty string")
            merged["wake_words"] = words
        for num_key, min_val in (("cooldown_sec", 0.0), ("volume_step_db", 0.1)):
            if num_key in updates:
                v = updates[num_key]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < min_val:
                    raise ValueError(f"{num_key} must be finite and >= {min_val}")
                merged[num_key] = float(v)
        if "volume_min_db" in updates or "volume_max_db" in updates:
            vmin = updates.get("volume_min_db", merged["volume_min_db"])
            vmax = updates.get("volume_max_db", merged["volume_max_db"])
            if (
                not isinstance(vmin, (int, float)) or isinstance(vmin, bool) or not math.isfinite(vmin)
                or not isinstance(vmax, (int, float)) or isinstance(vmax, bool) or not math.isfinite(vmax)
                or vmin > vmax
            ):
                raise ValueError("volume_min_db and volume_max_db must be finite and volume_min_db <= volume_max_db")
            merged["volume_min_db"] = float(vmin)
            merged["volume_max_db"] = float(vmax)
        if "audio_targets" in updates:
            if not isinstance(updates["audio_targets"], list):
                raise ValueError("audio_targets must be a list")
            merged["audio_targets"] = _normalize_config({"audio_targets": updates["audio_targets"]})["audio_targets"]
        if "custom_commands" in updates:
            if not isinstance(updates["custom_commands"], list):
                raise ValueError("custom_commands must be a list")
            merged["custom_commands"] = _normalize_config({"custom_commands": updates["custom_commands"]})["custom_commands"]
        for str_key in ("away_message", "normal_message"):
            if str_key in updates:
                if not isinstance(updates[str_key], str):
                    raise ValueError(f"{str_key} must be a string")
                merged[str_key] = updates[str_key]

        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{target}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, target)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        if target == VOICE_COMMAND_CONFIG_FILE:
            _CONFIG_CACHE["key"] = _config_file_key(target)
            _CONFIG_CACHE["value"] = copy.deepcopy(merged)
        return merged


def _strip_wake_word(utterance, wake_words):
    text = str(utterance or "").strip()
    if not text:
        return None, None
    sorted_words = sorted(wake_words, key=len, reverse=True)
    lowered = text.lower()
    for ww in sorted_words:
        ww_low = ww.lower()
        idx = lowered.find(ww_low)
        if idx < 0:
            continue
        if idx > 0 and text[idx - 1] not in " \t\n、。,.!！?？「『":
            continue
        rest = text[idx + len(ww):].lstrip(" \t\n、。,.!！?？:：")
        if rest:
            return ww, rest
        return ww, ""
    return None, None


def match_command(utterance, cfg=None):
    """発話文字列からウェイクワードと最長一致コマンドを判定する。"""
    if cfg is None:
        cfg = load_voice_command_config()
    ww, rest = _strip_wake_word(utterance, cfg.get("wake_words") or ["コマンド"])
    if ww is None or not rest:
        return None

    rest_compact = _compact(rest)
    if not rest_compact:
        return None

    best_score = -1
    best_match = None

    # 1. audio_targets (基本操作自動展開)
    for target in cfg.get("audio_targets") or []:
        input_name = target.get("input_name", "")
        for alias in target.get("aliases") or []:
            alias_c = _compact(alias)
            if not alias_c:
                continue
            for kw, op in _AUDIO_OPS:
                kw_c = _compact(kw)
                combined = alias_c + kw_c
                exact = (rest_compact == combined)
                if exact or (combined in rest_compact) or (alias_c in rest_compact and kw_c in rest_compact):
                    score = len(alias_c) * 1000 + len(kw_c) * 10 + (100 if exact else 0)
                    if score > best_score:
                        best_score = score
                        best_match = {
                            "kind": "audio",
                            "action": op,
                            "alias": alias,
                            "input_name": input_name,
                            "wake_word": ww,
                        }

    # 2. custom_commands
    for cmd in cfg.get("custom_commands") or []:
        for alias in cmd.get("aliases") or []:
            alias_c = _compact(alias)
            if not alias_c:
                continue
            exact = (rest_compact == alias_c)
            if exact or (alias_c in rest_compact):
                score = len(alias_c) * 1000 + (100 if exact else 0)
                if score > best_score:
                    best_score = score
                    best_match = {
                        "kind": "custom",
                        "alias": alias,
                        "command": cmd,
                        "wake_word": ww,
                    }

    return best_match


def _default_obs_client_factory():
    obs_pw = load_twitch_config().get("obs_password", "")
    return ObsWebSocketClient(password=obs_pw, timeout=4.0)


def _record_history(entry):
    _HISTORY.appendleft(entry)


def get_history():
    with _EXEC_LOCK:
        return list(_HISTORY)


def get_obs_resources(obs_client_factory=None):
    factory = obs_client_factory or _default_obs_client_factory
    try:
        with factory() as client:
            inputs = [
                {
                    "name": item.get("inputName", ""),
                    "kind": item.get("inputKind", ""),
                }
                for item in client.get_input_list()
                if item.get("inputName")
            ]
            scenes = [
                item.get("sceneName", "")
                for item in client.get_scene_list()
                if item.get("sceneName")
            ]
            filters_by_source = {}
            source_names = [i["name"] for i in inputs] + [s for s in scenes if s not in {i["name"] for i in inputs}]
            for src in source_names:
                try:
                    flts = client.get_source_filter_list(src)
                    if flts:
                        filters_by_source[src] = [
                            {
                                "name": f.get("filterName", ""),
                                "kind": f.get("filterKind", ""),
                                "enabled": bool(f.get("filterEnabled", True)),
                            }
                            for f in flts
                            if f.get("filterName")
                        ]
                except ObsWsError:
                    pass
            hotkeys = client.get_hotkey_list()
            return {
                "connected": True,
                "inputs": inputs,
                "filters_by_source": filters_by_source,
                "hotkeys": hotkeys,
            }
    except Exception as e:
        return {
            "connected": False,
            "inputs": [],
            "filters_by_source": {},
            "hotkeys": [],
            "error": str(e),
        }


def process_utterance(
    utterance,
    *,
    is_test=False,
    now=None,
    obs_client_factory=None,
    config_path=None,
):
    """確定発話を受け取り、コマンド一致時は OBS 操作・結果通知を行う。"""
    global _LAST_EXEC_TS, _SAVED_TICKER_BEFORE_AWAY

    if now is None:
        now = time.monotonic()

    cfg = load_voice_command_config(config_path=config_path)
    if not cfg.get("enabled", True) and not is_test:
        return {"matched": False, "executed": False, "reason": "disabled"}

    matched = match_command(utterance, cfg=cfg)
    if matched is None:
        return {"matched": False, "executed": False, "reason": "no_match"}

    cooldown = float(cfg.get("cooldown_sec", 1.5))
    with _EXEC_LOCK:
        if not is_test and _LAST_EXEC_TS is not None and cooldown > 0 and (now - _LAST_EXEC_TS) < cooldown:
            return {
                "matched": True,
                "executed": False,
                "reason": "cooldown",
                "alias": matched["alias"],
            }
        if not is_test:
            _LAST_EXEC_TS = now

    factory = obs_client_factory or _default_obs_client_factory
    ok = True
    summary = ""
    error_msg = ""

    try:
        with factory() as client:
            if matched["kind"] == "audio":
                op = matched["action"]
                input_name = matched["input_name"]
                alias = matched["alias"]
                if op in ("volume_up", "volume_down"):
                    vol_data = client.get_input_volume(input_name)
                    step = float(cfg.get("volume_step_db", 2.0))
                    min_db = float(cfg.get("volume_min_db", -60.0))
                    max_db = float(cfg.get("volume_max_db", 0.0))
                    raw_db = vol_data.get("inputVolumeDb") if isinstance(vol_data, dict) else None
                    if isinstance(raw_db, (int, float)) and not isinstance(raw_db, bool) and math.isfinite(raw_db):
                        cur_db = float(raw_db)
                    else:
                        cur_db = min_db
                    delta = step if op == "volume_up" else -step
                    new_db = max(min_db, min(max_db, round(cur_db + delta, 1)))
                    client.set_input_volume(input_name, volume_db=new_db)
                    summary = f"🎤 {alias}: {cur_db:.1f} dB → {new_db:.1f} dB"
                else:
                    muted = (op == "mute")
                    client.set_input_mute(input_name, muted)
                    summary = f"🎤 {alias}: {'ミュート' if muted else 'ミュート解除'}"
            else:
                cmd = matched["command"]
                ctype = cmd["type"]
                alias = matched["alias"]
                if ctype == "filter":
                    src = cmd["source_name"]
                    for df in cmd.get("disable_filters") or []:
                        client.set_source_filter_enabled(src, df, False)
                    enabled = bool(cmd.get("enabled", True))
                    client.set_source_filter_enabled(src, cmd["filter_name"], enabled)
                    summary = f"🎤 {alias}: {'ON' if enabled else 'OFF'}"
                elif ctype == "hotkey":
                    client.trigger_hotkey_by_name(cmd["hotkey_name"])
                    summary = f"🎤 {alias}: 実行"
                elif ctype == "away":
                    mode = cmd["mode"]
                    twitch_cfg = load_twitch_config()
                    if mode == "on":
                        away_text = (
                            (cfg.get("away_message") or "").strip()
                            or (twitch_cfg.get("away_ticker_message") or "").strip()
                            or DEFAULT_AWAY_MESSAGE
                        )
                        cur_ticker = twitch_cfg.get("current_ticker", "")
                        with _EXEC_LOCK:
                            if _SAVED_TICKER_BEFORE_AWAY is None and cur_ticker != away_text:
                                _SAVED_TICKER_BEFORE_AWAY = cur_ticker
                        save_twitch_config({"current_ticker": away_text})
                        client.broadcast_custom_event({
                            "realm": "obs-stream-ticker-v1",
                            "type": "UPDATE_TICKER",
                            "text": away_text,
                        })
                        summary = "🎤 離席中モード: ON"
                    else:
                        with _EXEC_LOCK:
                            restore_text = _SAVED_TICKER_BEFORE_AWAY
                            _SAVED_TICKER_BEFORE_AWAY = None
                        if restore_text is None:
                            restore_text = cfg.get("normal_message", "")
                        save_twitch_config({"current_ticker": restore_text})
                        client.broadcast_custom_event({
                            "realm": "obs-stream-ticker-v1",
                            "type": "UPDATE_TICKER",
                            "text": restore_text,
                        })
                        summary = "🎤 離席中モード: OFF"

            client.broadcast_custom_event({
                "realm": "obs-voice-command-v1",
                "type": "VOICE_COMMAND_RESULT",
                "ok": True,
                "summary": summary,
                "utterance": utterance,
                "timestamp": time.time(),
            })
    except Exception as e:
        ok = False
        error_msg = str(e)
        summary = f"⚠️ {matched['alias']}: {error_msg}"

    entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "utterance": str(utterance),
        "alias": matched["alias"],
        "ok": ok,
        "summary": summary,
        "is_test": bool(is_test),
    }
    if error_msg:
        entry["error"] = error_msg
    with _EXEC_LOCK:
        _record_history(entry)

    return {
        "matched": True,
        "executed": True,
        "ok": ok,
        "summary": summary,
        "utterance": str(utterance),
        "error": error_msg,
    }
