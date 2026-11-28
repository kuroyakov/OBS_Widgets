"""
離席状態の独立リアクタ群 (server/away_reactors.py, Issue #75)

責務:
- 離席状態基盤 (server/away_state.py) の状態変化を購読し、各要素を独立に実行する。
  各リアクタは互いを直接呼ばず、離席状態の変更も巻き戻しもしない。
- TickerAwayReactor:
  - ON で現在の current_ticker を退避し、離席文言を保存して UPDATE_TICKER を放送する。
  - OFF で退避文言（無ければ通常文言）を復帰保存して放送する。
  - reason が scene_change / manual_ticker_change の OFF では、新しい文言を退避文言で
    上書きしない (T2)。
- MicMuteAwayReactor:
  - ON で対象マイクが未ミュートなら OBS SetInputMute(True) を実行し、「自分がミュートした」
    事実を away_mic_mute_state.json へアトミックに記録する (T4)。
  - OFF では記録がある場合のみ SetInputMute(False) で復帰し、記録を消す（手動ミュートは
    解除しない, T2）。一時エラー時は記録を保持し、恒久エラー（対象不存在等）時のみ破棄する。
  - 対象マイクは voice_command_config.json の away_mic_mute.input_name を参照し、
    空のときは subtitle_mute_link.input_name にフォールバックする。
  - OBS 未接続・対象マイク不在・設定未指定のときはログを出して no-op とする。
- import 時にはリアクタ登録・スレッド起動を行わない (T7)。
"""

import json
import os
import re
import sys
import threading
import time

from server.away_state import (
    get_away_state,
    register_away_reactor,
    unregister_away_reactor,
)
from server.config import (
    BASE_DIR,
    OBS_WS_UPSTREAM_HOST,
    OBS_WS_UPSTREAM_PORT,
    load_twitch_config,
    read_env_local_obs_password,
    save_twitch_config,
)
from server.obs_ws import ObsWebSocketClient, ObsWsError
from server.voice_command import (
    DEFAULT_AWAY_MESSAGE,
    load_voice_command_config,
)

AWAY_MIC_MUTE_STATE_FILE = os.path.join(BASE_DIR, "away_mic_mute_state.json")

_SKIP_TICKER_RESTORE_REASONS = frozenset({"scene_change", "manual_ticker_change"})

_PERMANENT_OBS_ERROR_CODES = frozenset({
    203,  # MissingRequestType
    204,  # UnknownRequestType
    206,  # UnsupportedRequestBatchExecutionType
    300,  # MissingRequestField
    301,  # MissingRequestData
    400,  # InvalidRequestField
    401,  # InvalidRequestFieldType
    402,  # RequestFieldOutOfRange
    403,  # RequestFieldEmpty
    404,  # TooManyRequestFields
    600,  # ResourceNotFound
    602,  # InvalidResourceType
    604,  # InvalidResourceState
    605,  # InvalidInputKind
    606,  # ResourceNotConfigurable
    607,  # InvalidFilterKind
})

_OBS_CODE_RE = re.compile(r"\bcode=(\d+)\b")


def _parse_bool_flag(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm == "true":
            return True
        if norm == "false":
            return False
    return default


def _extract_obs_error_code(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    match = _OBS_CODE_RE.search(str(exc))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _is_permanent_obs_error(exc):
    if not isinstance(exc, ObsWsError):
        return False
    return _extract_obs_error_code(exc) in _PERMANENT_OBS_ERROR_CODES


def _default_obs_client_factory():
    t_cfg = load_twitch_config()
    host = str(t_cfg.get("obs_ws_host") or "").strip() or OBS_WS_UPSTREAM_HOST
    port_raw = t_cfg.get("obs_ws_port")
    if isinstance(port_raw, int) and not isinstance(port_raw, bool) and 1 <= port_raw <= 65535:
        port = port_raw
    else:
        port = OBS_WS_UPSTREAM_PORT
    obs_pw = str(t_cfg.get("obs_password") or "") or read_env_local_obs_password()
    return ObsWebSocketClient(host=host, port=port, password=obs_pw, timeout=4.0)


def resolve_away_mic_mute_config(vc_cfg):
    """voice_command_config から離席時マイクミュートの (enabled, input_name) を解決する。

    away_mic_mute.input_name が空のときは subtitle_mute_link.input_name にフォールバックする。
    """
    if not isinstance(vc_cfg, dict):
        return True, ""
    raw_amm = vc_cfg.get("away_mic_mute")
    if isinstance(raw_amm, dict):
        if "enabled" in raw_amm:
            enabled = _parse_bool_flag(raw_amm.get("enabled"), default=False)
        else:
            enabled = True
        raw_input = raw_amm.get("input_name")
        input_name = raw_input.strip() if isinstance(raw_input, str) else ""
    else:
        enabled = True
        input_name = ""

    if not input_name:
        raw_sml = vc_cfg.get("subtitle_mute_link")
        if isinstance(raw_sml, dict):
            fallback_input = raw_sml.get("input_name")
            if isinstance(fallback_input, str) and fallback_input.strip():
                input_name = fallback_input.strip()

    return enabled, input_name


def load_mic_mute_state(state_path=None):
    """away_mic_mute_state.json から自分がミュートした記録を読み込む。不在・破損時は None。"""
    target = state_path or AWAY_MIC_MUTE_STATE_FILE
    if not os.path.exists(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(
            f"[away_reactors] マイクミュート状態ファイルの読み込みに失敗しました ({exc})",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        print(
            "[away_reactors] マイクミュート状態ファイルの形式が不正です (dict ではありません)",
            file=sys.stderr,
        )
        return None
    if data.get("muted_by_away") is not True:
        return None
    raw_input = data.get("input_name")
    if not isinstance(raw_input, str) or not raw_input.strip():
        return None
    return {
        "version": 1,
        "muted_by_away": True,
        "input_name": raw_input.strip(),
        "updated_at": data.get("updated_at", 0.0),
    }


def save_mic_mute_state(input_name, state_path=None):
    """自分がミュートした記録をアトミック書き込みで保存する (T4)。"""
    target = state_path or AWAY_MIC_MUTE_STATE_FILE
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "version": 1,
        "muted_by_away": True,
        "input_name": str(input_name).strip(),
        "updated_at": time.time(),
    }
    tmp_path = f"{target}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def clear_mic_mute_state(state_path=None):
    """自分がミュートした記録を削除する。"""
    target = state_path or AWAY_MIC_MUTE_STATE_FILE
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"[away_reactors] マイクミュート状態ファイルの削除に失敗しました ({exc})",
            file=sys.stderr,
        )


class TickerAwayReactor:
    """離席状態に応じてティッカー文言を退避・切替・復帰する独立リアクタ。"""

    def __init__(
        self,
        *,
        obs_client_factory=None,
        load_vc_config=None,
        load_tw_config=None,
        save_tw_config=None,
        get_current_seq=None,
    ):
        self._obs_client_factory = obs_client_factory
        self._load_vc_config = load_vc_config
        self._load_tw_config = load_tw_config
        self._save_tw_config = save_tw_config
        self._get_current_seq = get_current_seq
        self._lock = threading.Lock()
        self._saved_ticker = None
        self._applied_away = False

    def _current_seq(self):
        fn = self._get_current_seq or (lambda: get_away_state()["seq"])
        return fn()

    def _is_stale(self, seq):
        if seq is None:
            return False
        return self._current_seq() != seq

    @staticmethod
    def _resolve_away_text(vc_cfg, tw_cfg):
        return (
            (vc_cfg.get("away_message") or "").strip()
            or (tw_cfg.get("away_ticker_message") or "").strip()
            or DEFAULT_AWAY_MESSAGE
        )

    def _broadcast_ticker(self, text):
        factory = self._obs_client_factory or _default_obs_client_factory
        try:
            with factory() as client:
                client.broadcast_custom_event({
                    "realm": "obs-stream-ticker-v1",
                    "type": "UPDATE_TICKER",
                    "text": text,
                })
        except Exception as exc:
            print(
                f"[away_reactors] UPDATE_TICKER の放送に失敗しました (離席状態は維持します): {exc}",
                file=sys.stderr,
            )

    def on_away_state(self, state):
        active = bool(state.get("active"))
        reason = str(state.get("reason") or "").strip()
        seq = state.get("seq")
        load_vc = self._load_vc_config or load_voice_command_config
        load_tw = self._load_tw_config or load_twitch_config
        save_tw = self._save_tw_config or save_twitch_config

        with self._lock:
            if self._is_stale(seq):
                return

            if active:
                vc_cfg = load_vc()
                tw_cfg = load_tw()
                away_text = self._resolve_away_text(vc_cfg, tw_cfg)
                cur_ticker = tw_cfg.get("current_ticker", "")
                if self._is_stale(seq):
                    return
                if self._saved_ticker is None and cur_ticker != away_text:
                    self._saved_ticker = cur_ticker
                save_tw({"current_ticker": away_text})
                self._applied_away = True
                self._broadcast_ticker(away_text)
                return

            # OFF 時: シーン切替・手動ティッカー変更で解除された場合は新文言を上書きしない (T2)
            if reason in _SKIP_TICKER_RESTORE_REASONS:
                self._saved_ticker = None
                self._applied_away = False
                return

            if self._is_stale(seq):
                return

            # 自分が離席文言を適用していない場合は既存ティッカーを上書きしない (T2)
            if not self._applied_away:
                self._saved_ticker = None
                return

            restore_text = self._saved_ticker
            if restore_text is None:
                vc_cfg = load_vc()
                tw_cfg = load_tw()
                away_text = self._resolve_away_text(vc_cfg, tw_cfg)
                if tw_cfg.get("current_ticker", "") != away_text:
                    self._applied_away = False
                    return
                restore_text = vc_cfg.get("normal_message", "")

            if self._is_stale(seq):
                return
            self._saved_ticker = None
            self._applied_away = False
            save_tw({"current_ticker": restore_text})
            self._broadcast_ticker(restore_text)


class MicMuteAwayReactor:
    """離席状態に応じて対象マイクのミュート・復帰を行う独立リアクタ。"""

    def __init__(
        self,
        *,
        obs_client_factory=None,
        load_vc_config=None,
        state_path=None,
        get_current_seq=None,
    ):
        self._obs_client_factory = obs_client_factory
        self._load_vc_config = load_vc_config
        self._state_path = state_path
        self._get_current_seq = get_current_seq
        self._lock = threading.Lock()

    def _current_seq(self):
        fn = self._get_current_seq or (lambda: get_away_state()["seq"])
        return fn()

    def _is_stale(self, seq):
        if seq is None:
            return False
        return self._current_seq() != seq

    def on_away_state(self, state):
        active = bool(state.get("active"))
        seq = state.get("seq")
        load_vc = self._load_vc_config or load_voice_command_config
        factory = self._obs_client_factory or _default_obs_client_factory

        with self._lock:
            if self._is_stale(seq):
                return

            if active:
                vc_cfg = load_vc()
                enabled, input_name = resolve_away_mic_mute_config(vc_cfg)
                if not enabled:
                    return
                if not input_name:
                    print(
                        "[away_reactors] 離席時マイクミュートの対象マイクが未設定のためスキップします",
                        file=sys.stderr,
                    )
                    return
                if self._is_stale(seq):
                    return
                try:
                    with factory() as client:
                        already_muted = bool(client.get_input_mute(input_name))
                        if already_muted:
                            print(
                                f"[away_reactors] 対象マイク '{input_name}' は既にミュート済みのため記録せず維持します",
                                file=sys.stderr,
                            )
                            return
                        if self._is_stale(seq):
                            return
                        client.set_input_mute(input_name, True)
                        save_mic_mute_state(input_name, state_path=self._state_path)
                except Exception as exc:
                    print(
                        f"[away_reactors] 離席時マイクミュートに失敗しました (no-op で継続します, input={input_name}): {exc}",
                        file=sys.stderr,
                    )
                return

            # OFF 時: 自分がミュートした記録がある場合のみ解除する (T2)
            record = load_mic_mute_state(state_path=self._state_path)
            if record is None:
                return
            if self._is_stale(seq):
                return
            input_name = record["input_name"]
            try:
                with factory() as client:
                    if self._is_stale(seq):
                        return
                    client.set_input_mute(input_name, False)
                clear_mic_mute_state(state_path=self._state_path)
            except Exception as exc:
                if _is_permanent_obs_error(exc):
                    print(
                        f"[away_reactors] 復帰対象マイクが存在しないため記録を破棄します (input={input_name}): {exc}",
                        file=sys.stderr,
                    )
                    clear_mic_mute_state(state_path=self._state_path)
                else:
                    print(
                        f"[away_reactors] マイクミュート復帰で一時エラーが発生したため記録を保持します (input={input_name}): {exc}",
                        file=sys.stderr,
                    )


_REACTORS_LOCK = threading.Lock()
_TICKER_REACTOR = None
_MIC_MUTE_REACTOR = None


def start_away_reactors(
    *,
    obs_client_factory=None,
    load_vc_config=None,
    load_tw_config=None,
    save_tw_config=None,
    state_path=None,
    get_current_seq=None,
):
    """ティッカー・マイクミュートの独立リアクタを離席状態基盤へ登録する。"""
    global _TICKER_REACTOR, _MIC_MUTE_REACTOR
    with _REACTORS_LOCK:
        _TICKER_REACTOR = TickerAwayReactor(
            obs_client_factory=obs_client_factory,
            load_vc_config=load_vc_config,
            load_tw_config=load_tw_config,
            save_tw_config=save_tw_config,
            get_current_seq=get_current_seq,
        )
        _MIC_MUTE_REACTOR = MicMuteAwayReactor(
            obs_client_factory=obs_client_factory,
            load_vc_config=load_vc_config,
            state_path=state_path,
            get_current_seq=get_current_seq,
        )
        register_away_reactor("ticker", _TICKER_REACTOR.on_away_state)
        register_away_reactor("mic_mute", _MIC_MUTE_REACTOR.on_away_state)


def stop_away_reactors(timeout=2.0):
    """登録済みのティッカー・マイクミュートリアクタを解除・停止する。"""
    global _TICKER_REACTOR, _MIC_MUTE_REACTOR
    unregister_away_reactor("ticker", timeout=timeout)
    unregister_away_reactor("mic_mute", timeout=timeout)
    with _REACTORS_LOCK:
        _TICKER_REACTOR = None
        _MIC_MUTE_REACTOR = None
