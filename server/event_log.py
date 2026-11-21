"""
サーバー常設イベントログ (server/event_log.py)

Issue #23 の一時設備だった診断レコーダーのうち、サーバー側の記録だけを
正式なログとして残したもの (Issue #28)。タブ閉じの実行結果や逐次
リフレッシュの進行など、サーバーが観測できるイベントを日次 JSONL
(logs/event-YYYYMMDD.jsonl) へ追記する。書式は旧 diag 形式と同一
({ts, client, tag, data})。保持期間は RETENTION_DAYS 日で、古い
ファイル (旧 diag-*.jsonl を含む) は初回書き込み時に掃除する。
記録の失敗で本体を止めないよう、例外は握り潰す。
"""

import json
import os
import re
from datetime import datetime, timezone

from .config import BASE_DIR

LOG_DIR = os.path.join(BASE_DIR, "logs")
RETENTION_DAYS = 14

_LOG_FILE_RE = re.compile(r"^(?:event|diag)-(\d{8})\.jsonl$")
_pruned = False


def _prune_old_logs():
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        today = datetime.now().date()
        for name in os.listdir(LOG_DIR):
            m = _LOG_FILE_RE.match(name)
            if not m:
                continue
            try:
                file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if (today - file_date).days >= RETENTION_DAYS:
                try:
                    os.remove(os.path.join(LOG_DIR, name))
                except OSError:
                    pass
    except Exception:
        pass


def log_event(tag, data=None, client="server"):
    """イベントを1行追記する（失敗は握り潰す）"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _prune_old_logs()
        log_file = os.path.join(LOG_DIR, datetime.now().strftime("event-%Y%m%d.jsonl"))
        entry = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "client": client,
            "tag": tag,
            "data": data if data is not None else {},
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
