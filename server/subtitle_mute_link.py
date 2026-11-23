"""
マイクミュートと字幕ソース非表示の連動モジュール (server/subtitle_mute_link.py, Issue #39)

機能:
- voice_command_config.json の `subtitle_mute_link` セクションを読み込み、
  対象マイクの OBS ミュート状態に連動して指定した字幕ソースの表示/非表示を切り替える。
- 専用の OBS WebSocket (v5) 接続で `InputMuteStateChanged` を購読し、
  起動時・再接続時は `GetInputMute` で現在状態へ reconcile する。
- ミュート時は全シーン直下のアイテムのうち「現在表示中」の対象ソースだけを
  `subtitle_mute_link_state.json` へアトミック記録してから非表示にする。
- ミュート解除時は記録したアイテムのみをソース名照合のうえで再表示し、
  手動で非表示にしているソースや対象外ソースには触れない。
- 一時的なエラー（OBS の NotReady や切断等）では記録を保持して後の reconcile で再試行し、
  恒久的な拒否（対象不存在等）やソース名不一致時は記録を破棄する。
"""

import json
import os
import re
import sys
import threading
import time

from server.config import (
    BASE_DIR,
    OBS_WS_UPSTREAM_HOST,
    OBS_WS_UPSTREAM_PORT,
    load_twitch_config,
)
from server.obs_ws import (
    EVENT_SUBSCRIPTION_INPUTS,
    ObsWebSocketClient,
    ObsWsError,
)
from server.voice_command import (
    _read_env_local_obs_password,
    load_voice_command_config,
)

STATE_FILE = os.path.join(BASE_DIR, "subtitle_mute_link_state.json")

DEFAULT_RECONNECT_INTERVAL_SEC = 2.0
DEFAULT_POLL_SLICE_SEC = 0.5
DEFAULT_RETRY_RECONCILE_SEC = 2.0
MAX_RECONCILE_VERIFY_PASSES = 5

# OBS WebSocket v5 の恒久的な拒否コード（再試行しても解決しない対象不存在・不正リクエスト等）。
# ここに含まれないコード（例: 207 NotReady、205 GenericError、700系）および
# コード無し（切断・タイムアウト等の通信異常）は一時エラー扱いとし、復帰記録を保持する。
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


def parse_bool_flag(value):
    """enabled を安全に解釈する。真偽値が基本、文字列は 'true'/'false' のみ受理し他は無効扱い。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm == "true":
            return True
        if norm == "false":
            return False
    return False


def extract_link_config(voice_cmd_cfg):
    """voice_command_config 辞書から subtitle_mute_link の正規化済み設定を取り出す。"""
    if not isinstance(voice_cmd_cfg, dict):
        return {"enabled": False, "input_name": "", "source_names": []}
    raw = voice_cmd_cfg.get("subtitle_mute_link")
    if not isinstance(raw, dict):
        return {"enabled": False, "input_name": "", "source_names": []}

    enabled = parse_bool_flag(raw.get("enabled", False))
    raw_input = raw.get("input_name")
    input_name = raw_input.strip() if isinstance(raw_input, str) else ""

    source_names = []
    seen = set()
    raw_sources = raw.get("source_names")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            if isinstance(item, str):
                name = item.strip()
                if name and name not in seen:
                    seen.add(name)
                    source_names.append(name)

    return {
        "enabled": enabled,
        "input_name": input_name,
        "source_names": source_names,
    }


def is_link_config_active(link_cfg):
    """連動監視を起動すべき有効な設定かどうかを返す。"""
    return bool(
        isinstance(link_cfg, dict)
        and link_cfg.get("enabled") is True
        and link_cfg.get("input_name")
        and link_cfg.get("source_names")
    )


def extract_obs_error_code(exc):
    """例外から OBS WebSocket の RequestStatus code を取り出す。取得できなければ None。"""
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


def is_permanent_obs_error(exc):
    """対象不存在など再試行しても解決しない恒久的な拒否かどうかを判定する。"""
    if not isinstance(exc, ObsWsError):
        return False
    code = extract_obs_error_code(exc)
    return code in _PERMANENT_OBS_ERROR_CODES


def is_transport_error(exc):
    """ソケット切断・タイムアウトなど WebSocket 接続自体の異常かどうかを判定する。"""
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, ObsWsError):
        return extract_obs_error_code(exc) is None
    return False


def load_hidden_state(state_path=None):
    """状態ファイルから連動で非表示にしたアイテム一覧を読み込む。"""
    target = state_path or STATE_FILE
    if not os.path.exists(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(
            f"[subtitle_mute_link] 状態ファイルの読み込みに失敗しました ({exc})",
            file=sys.stderr,
        )
        return []
    if not isinstance(data, dict):
        return []
    raw_items = data.get("hidden_items")
    if not isinstance(raw_items, list):
        return []

    items = []
    seen = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        scene = entry.get("scene_name")
        item_id = entry.get("scene_item_id")
        source = entry.get("source_name")
        if (
            not isinstance(scene, str)
            or not scene
            or not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or not isinstance(source, str)
            or not source
        ):
            continue
        key = (scene, item_id)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "scene_name": scene,
            "scene_item_id": item_id,
            "source_name": source,
        })
    return items


def save_hidden_state(hidden_items, state_path=None):
    """状態ファイルをアトミック書き込みで保存する。"""
    target = state_path or STATE_FILE
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "version": 1,
        "hidden_items": list(hidden_items),
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


def _default_obs_client_factory():
    """twitch_config.json / shared/env-config.local.js から接続先を解決し専用クライアントを返す。"""
    t_cfg = load_twitch_config()
    host = str(t_cfg.get("obs_ws_host") or "").strip() or OBS_WS_UPSTREAM_HOST
    port_raw = t_cfg.get("obs_ws_port")
    if isinstance(port_raw, int) and not isinstance(port_raw, bool) and 1 <= port_raw <= 65535:
        port = port_raw
    else:
        port = OBS_WS_UPSTREAM_PORT
    obs_pw = str(t_cfg.get("obs_password") or "") or _read_env_local_obs_password()
    return ObsWebSocketClient(
        host=host,
        port=port,
        password=obs_pw,
        timeout=4.0,
        event_subscriptions=EVENT_SUBSCRIPTION_INPUTS,
    )


def _hide_visible_targets(client, source_names, state_path, stop_event=None):
    """全シーン直下の表示中対象ソースを状態ファイルへ記録してから非表示にする。

    戻り値: had_transient_error (bool)
    """
    recorded = load_hidden_state(state_path)
    target_set = set(source_names)
    if not target_set:
        return False

    try:
        scenes = client.get_scene_list()
    except ObsWsError as exc:
        if is_transport_error(exc):
            raise
        if is_permanent_obs_error(exc):
            print(
                f"[subtitle_mute_link] シーン一覧の取得に失敗しました: {exc}",
                file=sys.stderr,
            )
            return False
        print(
            f"[subtitle_mute_link] シーン一覧の取得で一時エラーが発生しました: {exc}",
            file=sys.stderr,
        )
        return True

    to_hide = []
    seen_to_hide = set()
    had_transient = False

    for scene in scenes or []:
        if stop_event is not None and stop_event.is_set():
            return had_transient
        if not isinstance(scene, dict):
            continue
        scene_name = scene.get("sceneName")
        if not isinstance(scene_name, str) or not scene_name:
            continue
        try:
            resp = client.request("GetSceneItemList", {"sceneName": scene_name})
            scene_items = resp.get("sceneItems", []) if isinstance(resp, dict) else []
        except ObsWsError as exc:
            if is_transport_error(exc):
                raise
            if is_permanent_obs_error(exc):
                continue
            print(
                f"[subtitle_mute_link] シーンアイテム取得で一時エラー ({scene_name}): {exc}",
                file=sys.stderr,
            )
            had_transient = True
            continue

        for item in scene_items:
            if not isinstance(item, dict):
                continue
            src = item.get("sourceName")
            item_id = item.get("sceneItemId")
            enabled = item.get("sceneItemEnabled")
            if (
                src in target_set
                and enabled is True
                and isinstance(item_id, int)
                and not isinstance(item_id, bool)
            ):
                key = (scene_name, item_id)
                if key not in seen_to_hide:
                    seen_to_hide.add(key)
                    to_hide.append({
                        "scene_name": scene_name,
                        "scene_item_id": item_id,
                        "source_name": src,
                    })

    if not to_hide:
        return had_transient

    # 非表示操作の前に必ず状態ファイルへ記録する（既存記録は維持してマージ）
    merged_by_key = {}
    ordered_keys = []
    for entry in recorded:
        key = (entry["scene_name"], entry["scene_item_id"])
        if key not in merged_by_key:
            ordered_keys.append(key)
        merged_by_key[key] = entry
    for entry in to_hide:
        key = (entry["scene_name"], entry["scene_item_id"])
        if key not in merged_by_key:
            ordered_keys.append(key)
        merged_by_key[key] = entry

    merged = [merged_by_key[k] for k in ordered_keys]
    save_hidden_state(merged, state_path)

    permanently_removed = set()
    for entry in to_hide:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            client.request(
                "SetSceneItemEnabled",
                {
                    "sceneName": entry["scene_name"],
                    "sceneItemId": entry["scene_item_id"],
                    "sceneItemEnabled": False,
                },
            )
        except ObsWsError as exc:
            if is_transport_error(exc):
                raise
            if is_permanent_obs_error(exc):
                print(
                    f"[subtitle_mute_link] 非表示対象が存在しないため記録から除外します "
                    f"({entry['scene_name']}#{entry['scene_item_id']}): {exc}",
                    file=sys.stderr,
                )
                permanently_removed.add((entry["scene_name"], entry["scene_item_id"]))
            else:
                print(
                    f"[subtitle_mute_link] 非表示化で一時エラーが発生しました "
                    f"({entry['scene_name']}#{entry['scene_item_id']}): {exc}",
                    file=sys.stderr,
                )
                had_transient = True

    if permanently_removed:
        updated = [
            entry
            for entry in merged
            if (entry["scene_name"], entry["scene_item_id"]) not in permanently_removed
        ]
        save_hidden_state(updated, state_path)

    return had_transient


def _restore_hidden_items(client, state_path, stop_event=None):
    """記録済みのアイテムのみをソース名確認のうえで再表示し、成功分を状態ファイルから消す。

    一時的なエラー時は記録を保持して後の reconcile で再試行できるようにする。
    戻り値: had_transient_error (bool)
    """
    recorded = load_hidden_state(state_path)
    if not recorded:
        return False

    scene_items_cache = {}
    remaining = []
    changed = False
    had_transient = False

    for idx, entry in enumerate(recorded):
        if stop_event is not None and stop_event.is_set():
            remaining.extend(recorded[idx:])
            break

        scene_name = entry["scene_name"]
        item_id = entry["scene_item_id"]
        expected_source = entry["source_name"]

        if scene_name not in scene_items_cache:
            try:
                resp = client.request("GetSceneItemList", {"sceneName": scene_name})
                items = resp.get("sceneItems", []) if isinstance(resp, dict) else []
                scene_items_cache[scene_name] = ("ok", items)
            except ObsWsError as exc:
                if is_transport_error(exc):
                    remaining.extend(recorded[idx:])
                    if changed:
                        save_hidden_state(remaining, state_path)
                    raise
                if is_permanent_obs_error(exc):
                    print(
                        f"[subtitle_mute_link] 復帰先シーンが存在しないため記録を破棄します "
                        f"({scene_name}): {exc}",
                        file=sys.stderr,
                    )
                    scene_items_cache[scene_name] = ("permanent", None)
                else:
                    print(
                        f"[subtitle_mute_link] 復帰前確認で一時エラーが発生したため記録を保持します "
                        f"({scene_name}): {exc}",
                        file=sys.stderr,
                    )
                    scene_items_cache[scene_name] = ("transient", None)

        status, cached_items = scene_items_cache[scene_name]
        if status == "permanent":
            changed = True
            continue
        if status == "transient":
            remaining.append(entry)
            had_transient = True
            continue

        matched_item = None
        for item in cached_items:
            if isinstance(item, dict) and item.get("sceneItemId") == item_id:
                matched_item = item
                break

        if matched_item is None or matched_item.get("sourceName") != expected_source:
            # 対象アイテム不存在またはソース名不一致: 記録を捨てて触らない
            changed = True
            continue

        try:
            client.request(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene_name,
                    "sceneItemId": item_id,
                    "sceneItemEnabled": True,
                },
            )
            changed = True
        except ObsWsError as exc:
            if is_transport_error(exc):
                remaining.extend(recorded[idx:])
                if changed:
                    save_hidden_state(remaining, state_path)
                raise
            if is_permanent_obs_error(exc):
                print(
                    f"[subtitle_mute_link] 復帰対象が存在しないため記録を破棄します "
                    f"({scene_name}#{item_id}): {exc}",
                    file=sys.stderr,
                )
                changed = True
            else:
                print(
                    f"[subtitle_mute_link] 復帰時に一時エラーが発生したため記録を保持します "
                    f"({scene_name}#{item_id}): {exc}",
                    file=sys.stderr,
                )
                remaining.append(entry)
                had_transient = True

    if changed:
        save_hidden_state(remaining, state_path)

    return had_transient


def apply_mute_state(
    client,
    input_name,
    source_names,
    state_path,
    muted,
    *,
    stop_event=None,
    max_passes=MAX_RECONCILE_VERIFY_PASSES,
):
    """指定ミュート状態を適用し、適用後に GetInputMute を再確認して反転を取りこぼさない。

    戻り値: had_transient_error (bool)
    """
    current_muted = bool(muted)
    had_transient = False

    for _ in range(max(1, int(max_passes))):
        if stop_event is not None and stop_event.is_set():
            return had_transient

        if current_muted:
            transient = _hide_visible_targets(
                client, source_names, state_path, stop_event=stop_event
            )
        else:
            transient = _restore_hidden_items(
                client, state_path, stop_event=stop_event
            )
        had_transient = had_transient or transient

        if stop_event is not None and stop_event.is_set():
            return had_transient

        try:
            after_muted = bool(client.get_input_mute(input_name))
        except ObsWsError as exc:
            if is_transport_error(exc):
                raise
            if is_permanent_obs_error(exc):
                return had_transient
            print(
                f"[subtitle_mute_link] 適用後のミュート再確認で一時エラー ({input_name}): {exc}",
                file=sys.stderr,
            )
            return True

        if after_muted == current_muted:
            return had_transient
        current_muted = after_muted

    return had_transient


def reconcile(
    client,
    input_name,
    source_names,
    state_path,
    *,
    stop_event=None,
    max_passes=MAX_RECONCILE_VERIFY_PASSES,
):
    """現在の GetInputMute 状態に合わせて同期（reconcile）する。

    戻り値: had_transient_error (bool)
    """
    try:
        current_muted = bool(client.get_input_mute(input_name))
    except ObsWsError as exc:
        if is_transport_error(exc):
            raise
        if is_permanent_obs_error(exc):
            print(
                f"[subtitle_mute_link] 対象マイク '{input_name}' のミュート状態を取得できませんでした: {exc}",
                file=sys.stderr,
            )
            return False
        print(
            f"[subtitle_mute_link] ミュート状態取得で一時エラーが発生しました ({input_name}): {exc}",
            file=sys.stderr,
        )
        return True

    return apply_mute_state(
        client,
        input_name,
        source_names,
        state_path,
        current_muted,
        stop_event=stop_event,
        max_passes=max_passes,
    )


class SubtitleMuteLinkService:
    """OBS ミュート監視スレッドのライフサイクルと再構成を管理するサービス。"""

    def __init__(
        self,
        *,
        config_loader=None,
        client_factory=None,
        state_path=None,
        reconnect_interval=DEFAULT_RECONNECT_INTERVAL_SEC,
        poll_slice=DEFAULT_POLL_SLICE_SEC,
        retry_reconcile_interval=DEFAULT_RETRY_RECONCILE_SEC,
    ):
        self._config_loader = config_loader or load_voice_command_config
        self._client_factory = client_factory or _default_obs_client_factory
        self._state_path = state_path or STATE_FILE
        self._reconnect_interval = float(reconnect_interval)
        self._poll_slice = float(poll_slice)
        self._retry_reconcile_interval = float(retry_reconcile_interval)

        self._ctrl_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._thread = None
        self._stop_event = None
        self._active_client = None
        self._active_signature = None

    def _load_active_config(self):
        try:
            raw_cfg = self._config_loader()
        except Exception as exc:
            print(
                f"[subtitle_mute_link] 設定の読み込みに失敗しました (no-op で継続します): {exc}",
                file=sys.stderr,
            )
            return None
        link_cfg = extract_link_config(raw_cfg)
        if not is_link_config_active(link_cfg):
            return None
        return link_cfg

    @staticmethod
    def _signature(link_cfg):
        if not link_cfg:
            return None
        return (link_cfg["input_name"], tuple(link_cfg["source_names"]))

    def _stop_worker_locked(self, join_timeout=3.0):
        thread = self._thread
        stop_event = self._stop_event
        self._thread = None
        self._stop_event = None
        self._active_signature = None

        if stop_event is not None:
            stop_event.set()

        with self._client_lock:
            client = self._active_client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    def start(self):
        """現在の設定に応じて監視を開始する（無効・未設定時は完全な no-op）。"""
        return self.reconfigure()

    def reconfigure(self):
        """設定を再読み込みし、必要に応じて監視スレッドを停止・起動・再構成する。"""
        with self._ctrl_lock:
            link_cfg = self._load_active_config()
            new_sig = self._signature(link_cfg)

            if new_sig == self._active_signature:
                if new_sig is None:
                    return False
                if self._thread is not None and self._thread.is_alive():
                    return True

            self._stop_worker_locked()

            if link_cfg is None:
                return False

            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_loop,
                args=(link_cfg, stop_event),
                name="subtitle-mute-link",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            self._active_signature = new_sig
            thread.start()
            return True

    def stop(self):
        """監視スレッドを安全に停止する。"""
        with self._ctrl_lock:
            self._stop_worker_locked()

    def is_running(self):
        with self._ctrl_lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_loop(self, link_cfg, stop_event):
        input_name = link_cfg["input_name"]
        source_names = tuple(link_cfg["source_names"])

        while not stop_event.is_set():
            client = None
            try:
                client = self._client_factory()
                with self._client_lock:
                    if stop_event.is_set():
                        break
                    self._active_client = client

                client.connect()
                # 接続直後の停止チェック（connect 待機中に stop/reconfigure された場合は即終了）
                if stop_event.is_set():
                    break

                needs_retry = reconcile(
                    client,
                    input_name,
                    source_names,
                    self._state_path,
                    stop_event=stop_event,
                )
                next_retry_at = (
                    time.monotonic() + self._retry_reconcile_interval
                    if needs_retry
                    else None
                )

                while not stop_event.is_set():
                    msg = client.poll_message(timeout=self._poll_slice)
                    if stop_event.is_set():
                        break

                    if isinstance(msg, dict) and msg.get("op") == 5:
                        data = msg.get("d") or {}
                        if data.get("eventType") == "InputMuteStateChanged":
                            ev = data.get("eventData") or {}
                            if ev.get("inputName") == input_name and "inputMuted" in ev:
                                transient = apply_mute_state(
                                    client,
                                    input_name,
                                    source_names,
                                    self._state_path,
                                    bool(ev.get("inputMuted")),
                                    stop_event=stop_event,
                                )
                                next_retry_at = (
                                    time.monotonic() + self._retry_reconcile_interval
                                    if transient
                                    else None
                                )

                    if next_retry_at is not None and time.monotonic() >= next_retry_at:
                        needs_retry = reconcile(
                            client,
                            input_name,
                            source_names,
                            self._state_path,
                            stop_event=stop_event,
                        )
                        next_retry_at = (
                            time.monotonic() + self._retry_reconcile_interval
                            if needs_retry
                            else None
                        )
            except Exception as exc:
                if not stop_event.is_set():
                    print(
                        f"[subtitle_mute_link] OBS 監視エラー (再接続します): {exc}",
                        file=sys.stderr,
                    )
            finally:
                with self._client_lock:
                    if self._active_client is client:
                        self._active_client = None
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

            if not stop_event.is_set():
                stop_event.wait(timeout=self._reconnect_interval)


_SERVICE = SubtitleMuteLinkService()


def start_subtitle_mute_link():
    """サーバー起動時に呼ばれるエントリポイント。設定無効時は no-op。"""
    return _SERVICE.start()


def reconfigure_subtitle_mute_link():
    """設定保存後にサーバー再起動なしで監視を再構成する。"""
    return _SERVICE.reconfigure()


def stop_subtitle_mute_link():
    """サーバー終了時に監視スレッドを停止する。"""
    _SERVICE.stop()
