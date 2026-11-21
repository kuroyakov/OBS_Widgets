"""
コメント・アラート履歴ログ (server/feed_log.py)

UC-09 (Issue #133)。CommentViewer が受信したコメント・アラート・
配信マーカー・削除マーカーを、サーバー側で logs/feed-YYYYMMDD.jsonl
へ1行1 JSON で追記する。振り返り専用の私的記録で、外部送信はしない。

設計:
- 追記型のみ (既存行は書き換えない)。削除マーカーも別行として追記する
- 保持は無期限。event_log の自動掃除 (14日) の対象には含めない
  (ファイル名が event-/diag- の正規表現に一致しないことで自然に外れる)
- 記録は配信中のみ。stream マーカー (start/end) でセッションを開閉し、
  セッション外の comment / alert / deletion は受け付けない
- セッション状態は再起動時に直近のログ末尾から復元する (Issue #179)。
  最後のマーカーが start で 24 時間以内なら開いた状態に戻し、その
  セッション内の id も重複排除用に復元する。終了マーカーを取りこぼした
  古い start を「配信中」と誤復元しないため、古いマーカーは復元しない。
  24 時間以内の未クローズ start については Twitch API で現在の配信状態を
  確認し、オフラインなら復元しない (PR #194 round-1 指摘 1)。Twitch の
  配信状態が判定不能な場合 (未設定・API 到達不可など) は復元する。
  配信中のリロード継続を優先し、Twitch API の障害で記録が止まらない
  ようにするためのフォールバック方針
- 不正 UTF-8 行を含むログでも復元が例外で落ちないよう、読み込み時に
  errors="replace" を指定する (PR #194 round-1 指摘 2)
- 重複排除: レコードの id をセッション中保持し、同一 id の再送を弾く
  (複数経路・複数 CommentViewer インスタンスからの二重送信対策)
- 記録の失敗で本体 (CommentViewer の表示や他の API) を止めないよう、
  例外は握り潰す (event_log と同じ方針)
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from .config import BASE_DIR, load_twitch_config
from .twitch_api import twitch_request

LOG_DIR = os.path.join(BASE_DIR, "logs")

_VALID_KINDS = {"comment", "alert", "stream", "deletion"}

# 復元対象とする開始マーカーの新しさの上限 (Issue #179)
_RESTORE_MAX_AGE = timedelta(hours=24)
# 復元時に走査するログファイル数 (日跨ぎ配信で開始マーカーが前日の
# ファイルにある場合に備える)
_RESTORE_FILE_COUNT = 2

_lock = threading.Lock()
_session_open = False
_seen_ids = set()
_state_restored = False


def _append_line(entry):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, datetime.now().strftime("feed-%Y%m%d.jsonl"))
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ts_is_recent(ts):
    if not isinstance(ts, str):
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= _RESTORE_MAX_AGE


def _twitch_stream_is_live():
    """Twitch の現在の配信状態を確認する (PR #194 round-1 指摘 1)。

    戻り値: True=配信中 / False=オフライン / None=判定不能
    (Twitch 未設定・broadcaster_id 未設定・API 到達不可など)。
    判定不能時は None を返し、呼び出し側は復元する (フォールバック方針)。
    """
    try:
        config = load_twitch_config()
    except Exception:
        return None
    bid = (config.get("broadcaster_id") or "").strip()
    if not bid:
        return None
    try:
        data, code = twitch_request("streams?user_id=" + bid)
    except Exception:
        return None
    if code != 200 or not isinstance(data, dict):
        return None
    streams = data.get("data")
    if not isinstance(streams, list):
        return None
    return len(streams) > 0


def _restore_state_locked():
    """再起動時に直近のログ末尾からセッション状態を復元する (Issue #179)。

    最後に記録された stream マーカーが start で、その時刻が
    _RESTORE_MAX_AGE 以内ならセッションを開いた状態に戻し、その
    セッション内のレコード id を重複排除用に復元する。
    ただし Twitch API で現在オフラインと確認できた場合は、終了マーカーの
    取りこぼしと判断して復元しない。Twitch の状態が判定不能な場合は復元
    する (フォールバック方針はモジュール docstring を参照)。
    呼び出し側で _lock を保持していること。
    """
    global _session_open, _state_restored
    _state_restored = True
    try:
        names = sorted(
            n for n in os.listdir(LOG_DIR)
            if n.startswith("feed-") and n.endswith(".jsonl")
        )
    except OSError:
        return
    entries = []
    for name in names[-_RESTORE_FILE_COUNT:]:
        try:
            with open(os.path.join(LOG_DIR, name), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    last_start_idx = -1
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("kind") == "stream":
            if e.get("event") == "start":
                last_start_idx = i
            elif e.get("event") == "end":
                last_start_idx = -1
    if last_start_idx < 0:
        return
    if not _ts_is_recent(entries[last_start_idx].get("ts")):
        return
    if _twitch_stream_is_live() is False:
        # 終了マーカーを取りこぼした start: Twitch はオフラインなので
        # 復元しない (配信外の記録が混入するのを防ぐ)。
        print("feed_log: 未クローズの start がありますが Twitch はオフラインのためセッションを復元しません")
        return
    _session_open = True
    for e in entries[last_start_idx + 1:]:
        if isinstance(e, dict) and e.get("id"):
            _seen_ids.add(str(e["id"]))


def session_open():
    """現在のセッション状態を返す。未復元なら復元してから返す。"""
    with _lock:
        if not _state_restored:
            _restore_state_locked()
        return _session_open


def record_events(records):
    """レコード (dict のリスト) を検証して追記し、実際に記録した件数を返す。

    1件ずつ独立に判定するため、不正なレコードが混ざっても他は記録する。
    """
    global _session_open
    recorded = 0
    if not isinstance(records, list):
        return 0
    with _lock:
        if not _state_restored:
            _restore_state_locked()
        for rec in records:
            try:
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("kind")
                if kind not in _VALID_KINDS:
                    continue

                if kind == "stream":
                    event = rec.get("event")
                    if event == "start":
                        _session_open = True
                        _seen_ids.clear()
                    elif event == "end":
                        # 開始を取りこぼしても終了マーカー自体は残す
                        _append_line(_with_ts(rec))
                        recorded += 1
                        _session_open = False
                        _seen_ids.clear()
                        continue
                    else:
                        continue
                    _append_line(_with_ts(rec))
                    recorded += 1
                    continue

                if not _session_open:
                    continue

                rec_id = rec.get("id")
                if rec_id:
                    if str(rec_id) in _seen_ids:
                        continue
                    _seen_ids.add(str(rec_id))

                _append_line(_with_ts(rec))
                recorded += 1
            except Exception:
                continue
    return recorded


def _with_ts(rec):
    entry = dict(rec)
    entry["ts"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return entry
