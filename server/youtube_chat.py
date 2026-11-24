"""
YouTube Live チャット直接取得モジュール (server/youtube_chat.py)
- InnerTube 方式（APIキー不要・クォータ制限なし）による YouTube Live チャット・スパチャ・メンバーシップ検知
- バックグラウンドワーカースレッドでの継続ポーリングとイベント管理
- Viewer 初回ロード/リロード時 (since_seq < 0) の過去ログスキップ対応
- テスト発火用 InnerTube 生アクション注入 (inject_test_innertube_event)
"""

import re
import time
import json
import threading
import urllib.request
import urllib.parse

from .config import SSL_CONTEXT, load_twitch_config

YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cookie": "CONSENT=YES+cb.20210328-17-p0.en+FX+410"
}


ALLOWED_YOUTUBE_HOSTS = frozenset({
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "youtu.be",
})


def validate_youtube_handle(handle_or_url):
    """youtube_handle を検証する (Issue #49)。

    URL 入力時は https のみ、ホストは www.youtube.com / youtube.com /
    m.youtube.com / youtu.be のみ許可する。ハンドル (@name) や
    チャンネル ID (UC...) はそのまま許可する。
    """
    if not isinstance(handle_or_url, str):
        raise ValueError("youtube_handle は文字列で指定してください")
    raw = handle_or_url.strip()
    if not raw:
        return "@your_channel"

    if "://" in raw or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw):
        parsed = urllib.parse.urlsplit(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme != "https":
            raise ValueError(f"youtube_handle の URL スキームは https のみ許可されます: {parsed.scheme!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("youtube_handle に認証情報は指定できません")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"youtube_handle のポートが不正です: {exc}") from exc
        if port not in (None, 443):
            raise ValueError(f"youtube_handle に許可されていないポートです: {port}")
        host = (parsed.hostname or "").strip().lower()
        if host not in ALLOWED_YOUTUBE_HOSTS:
            raise ValueError(f"youtube_handle に許可されていないホストです: {host!r}")
        return raw

    if any(ch.isspace() for ch in raw):
        raise ValueError("youtube_handle に空白文字は指定できません")
    return raw


def build_youtube_live_url(handle_or_url, video_id_override=""):
    """チャンネルハンドル(@your_channel 等)・URL・動画IDから取得先URLを構築"""
    vid = (video_id_override or "").strip()
    if vid:
        m_v = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})", vid)
        if m_v:
            vid = m_v.group(1)
        if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
            return f"https://www.youtube.com/watch?v={vid}", vid

    raw = validate_youtube_handle(handle_or_url or "@your_channel")

    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.hostname or "").strip().lower()
        path = urllib.parse.unquote(parsed.path).rstrip("/")
        if not path.endswith("/live") and "/watch" not in path:
            path = path + "/live"
        safe_path = urllib.parse.quote(path, safe="/@-._~")
        return urllib.parse.urlunsplit(("https", host, safe_path, parsed.query, "")), ""

    if raw.startswith("UC") and len(raw) >= 20 and not raw.startswith("@"):
        return f"https://www.youtube.com/channel/{urllib.parse.quote(raw)}/live", ""

    handle = raw if raw.startswith("@") else f"@{raw}"
    decoded_handle = urllib.parse.unquote(handle)
    safe_handle = urllib.parse.quote(decoded_handle, safe="@-._~")
    return f"https://www.youtube.com/{safe_handle}/live", ""


def _extract_continuation_from_dict(obj):
    """ネストされた辞書/リストから liveChat 用の continuation トークンを再帰検索"""
    if isinstance(obj, dict):
        for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            if k in obj and isinstance(obj[k], dict):
                tok = obj[k].get("continuation")
                if tok:
                    return tok
        for v in obj.values():
            res = _extract_continuation_from_dict(v)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = _extract_continuation_from_dict(item)
            if res:
                return res
    return None


class YouTubeLiveChatManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"          # idle | connecting | live | offline | error
        self.video_id = ""
        self.channel_title = ""
        self.stream_title = ""
        self.api_key = ""
        self.continuation = None
        self.is_first_fetch = True
        self.last_client_poll_ts = 0.0
        self.last_connect_attempt_ts = 0.0
        self.seq = 0
        self.events = []              # 最大100件のリングバッファ
        self.seen_ids = set()
        self.seen_order = []
        self.error_msg = ""
        self.current_target_key = ""
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def touch_client_poll(self, since_seq=0):
        with self.lock:
            self.last_client_poll_ts = time.time()
            if since_seq < 0:
                # Viewer初回ロード・リロード時は過去バッファを返さず最新のseq番号のみ同期する
                new_events = []
            else:
                new_events = [ev for ev in self.events if ev["seq"] > since_seq]
            return {
                "status": self.status,
                "video_id": self.video_id,
                "stream_title": self.stream_title,
                "latest_seq": self.seq,
                "events": new_events,
                "error": self.error_msg
            }

    def force_reconnect(self):
        with self.lock:
            self.continuation = None
            self.video_id = ""
            self.status = "connecting"
            self.last_connect_attempt_ts = 0.0
            self.error_msg = ""
            self.events.clear()

    def _mark_seen(self, item_id):
        if not item_id:
            return False
        if item_id in self.seen_ids:
            return True
        self.seen_ids.add(item_id)
        self.seen_order.append(item_id)
        if len(self.seen_order) > 500:
            oldest = self.seen_order.pop(0)
            self.seen_ids.discard(oldest)
        return False

    def _append_event(self, payload):
        with self.lock:
            self.seq += 1
            ev = dict(payload)
            ev["seq"] = self.seq
            self.events.append(ev)
            if len(self.events) > 100:
                self.events = self.events[-100:]

    def _extract_photo(self, renderer):
        thumbs = (renderer.get("authorPhoto") or {}).get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            url = thumbs[-1].get("url", "")
            if url.startswith("//"):
                url = "https:" + url
            return url
        return ""

    def _ingest_actions(self, actions, first_fetch=False):
        """YouTube InnerTube の liveChatContinuation.actions 配列を解析してイベントキューへ追加"""
        added_count = 0
        for act in (actions or []):
            item = ((act or {}).get("addChatItemAction") or {}).get("item") or {}
            if not item:
                continue

            if "liveChatTextMessageRenderer" in item:
                r = item["liveChatTextMessageRenderer"]
                msg_id = r.get("id", "")
                if self._mark_seen(msg_id) or first_fetch:
                    continue
                author = (r.get("authorName") or {}).get("simpleText") or "YouTubeユーザー"
                badges, user_color = self._parse_badges(r.get("authorBadges"))
                fragments, raw_text = self._parse_runs((r.get("message") or {}).get("runs"))
                if not fragments and not raw_text:
                    continue
                self._append_event({
                    "event_type": "chat",
                    "platform": "youtube",
                    "messageId": f"yt-{msg_id}",
                    "userName": author,
                    "avatarUrl": self._extract_photo(r),
                    "userColor": user_color,
                    "badges": badges,
                    "fragments": fragments,
                    "rawText": raw_text
                })
                added_count += 1

            elif "liveChatPaidMessageRenderer" in item:
                r = item["liveChatPaidMessageRenderer"]
                msg_id = r.get("id", "")
                if self._mark_seen(msg_id) or first_fetch:
                    continue
                author = (r.get("authorName") or {}).get("simpleText") or "YouTubeユーザー"
                amount = (r.get("purchaseAmountText") or {}).get("simpleText") or ""
                _, msg_text = self._parse_runs((r.get("message") or {}).get("runs"))
                self._append_event({
                    "event_type": "alert",
                    "platform": "youtube",
                    "alertId": f"yt-sc-{msg_id}",
                    "kind": "cheer",
                    "typeLabel": f"💴 SUPER CHAT ({amount})" if amount else "💴 SUPER CHAT",
                    "titleText": f"{author} さんがスパチャを送りました！ {amount}",
                    "avatarUrl": self._extract_photo(r),
                    "messageText": msg_text,
                    "ttsText": f"YouTubeで{author}さんが{amount}のスーパーチャットを送りました！ {msg_text}"
                })
                added_count += 1

            elif "liveChatMembershipItemRenderer" in item:
                r = item["liveChatMembershipItemRenderer"]
                msg_id = r.get("id", "")
                if self._mark_seen(msg_id) or first_fetch:
                    continue
                author = (r.get("authorName") or {}).get("simpleText") or "YouTubeユーザー"
                _, header_sub = self._parse_runs((r.get("headerSubtext") or {}).get("runs"))
                _, msg_text = self._parse_runs((r.get("message") or {}).get("runs"))
                self._append_event({
                    "event_type": "alert",
                    "platform": "youtube",
                    "alertId": f"yt-mem-{msg_id}",
                    "kind": "sub",
                    "typeLabel": "⭐ YOUTUBE MEMBER",
                    "titleText": f"{author} さん ({header_sub or 'メンバーシップ'})",
                    "avatarUrl": self._extract_photo(r),
                    "messageText": msg_text,
                    "ttsText": f"YouTubeで{author}さんがメンバーシップに加入しました！ {msg_text}"
                })
                added_count += 1

        return added_count

    def inject_test_innertube_event(self, kind):
        """
        テスト発火用: YouTube InnerTube API の生の addChatItemAction ペイロードを生成し、
        意図的に同じアイテムを複数回（重複して） _ingest_actions に流し込んで
        サーバー側の重複排除 (_mark_seen) ＋ CommentViewer 側の /api/youtube/poll 受信を一貫検証する。
        """
        ts_id = f"test_{kind}_{int(time.time() * 1000)}"

        if kind == "yt-jp-chat":
            raw_action = {
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "id": ts_id,
                            "authorName": {"simpleText": "サンプルリスナーYT"},
                            "authorPhoto": {
                                "thumbnails": [{"url": "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?auto=format&fit=crop&w=80&q=80"}]
                            },
                            "authorBadges": [
                                {"liveChatAuthorBadgeRenderer": {"tooltip": "メンバー (6か月)"}}
                            ],
                            "message": {
                                "runs": [{"text": "YouTubeから観てます！高画質で鍵盤もスプラも見やすいです！"}]
                            }
                        }
                    }
                }
            }
        elif kind == "yt-en-chat":
            raw_action = {
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "id": ts_id,
                            "authorName": {"simpleText": "Alex_From_YouTube"},
                            "authorPhoto": {
                                "thumbnails": [{"url": "https://images.unsplash.com/photo-1570295999919-56ceb5ecca61?auto=format&fit=crop&w=80&q=80"}]
                            },
                            "authorBadges": [],
                            "message": {
                                "runs": [{"text": "Watching from YouTube Live! Your stream setup is awesome!"}]
                            }
                        }
                    }
                }
            }
        elif kind == "yt-superchat":
            raw_action = {
                "addChatItemAction": {
                    "item": {
                        "liveChatPaidMessageRenderer": {
                            "id": ts_id,
                            "authorName": {"simpleText": "YouTubeサポーター"},
                            "authorPhoto": {
                                "thumbnails": [{"url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=80&q=80"}]
                            },
                            "purchaseAmountText": {"simpleText": "￥1,000"},
                            "message": {
                                "runs": [{"text": "いつも楽しい配信ありがとうございます！応援してます！"}]
                            }
                        }
                    }
                }
            }
        elif kind == "yt-member":
            raw_action = {
                "addChatItemAction": {
                    "item": {
                        "liveChatMembershipItemRenderer": {
                            "id": ts_id,
                            "authorName": {"simpleText": "サンプルメンバーYT"},
                            "authorPhoto": {
                                "thumbnails": [{"url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?auto=format&fit=crop&w=80&q=80"}]
                            },
                            "headerSubtext": {"runs": [{"text": "新規メンバーシップ加入"}]},
                            "message": {
                                "runs": [{"text": "YouTubeメンバーシップ入りました！スタンプ使うの楽しみです！"}]
                            }
                        }
                    }
                }
            }
        else:
            return {"added": 0, "duplicates_blocked": 0}

        # YouTube側で同一アクションが3回重複して返ってきたケースをシミュレート
        duplicate_actions = [raw_action, raw_action, raw_action]
        added = self._ingest_actions(duplicate_actions, first_fetch=False)
        return {
            "added": added,
            "simulated_raw_count": len(duplicate_actions),
            "duplicates_blocked": len(duplicate_actions) - added,
            "latest_seq": self.seq
        }

    def _connect_live_chat(self, config):
        handle = config.get("youtube_handle") or "@your_channel"
        vid_override = config.get("youtube_video_id") or ""
        try:
            target_url, explicit_vid = build_youtube_live_url(handle, vid_override)
        except ValueError as e:
            with self.lock:
                self.status = "error"
                self.error_msg = f"YouTube設定エラー: {e}"
            return False

        req = urllib.request.Request(target_url, headers=YT_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            with self.lock:
                self.status = "error"
                self.error_msg = f"YouTube接続エラー: {e}"
            return False

        if '"liveChatRenderer"' not in html and not explicit_vid:
            with self.lock:
                self.status = "offline"
                self.video_id = ""
                self.stream_title = ""
                self.continuation = None
                self.error_msg = ""
            return False

        m_key = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
        api_key = m_key.group(1) if m_key else ""

        vid = explicit_vid
        if not vid:
            m_canon = re.search(r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"', html)
            if m_canon:
                vid = m_canon.group(1)
            else:
                m_vid = re.search(r'"videoDetails"\s*:\s*\{[^}]*"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', html)
                if m_vid:
                    vid = m_vid.group(1)

        m_title = re.search(r'<meta\s+name="title"\s+content="([^"]+)"', html)
        stream_title = m_title.group(1) if m_title else ""

        continuation = None
        m_init = re.search(r'var\s+ytInitialData\s*=\s*(\{.+?\});\s*</script>', html, re.S)
        if not m_init:
            m_init = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.+?\});', html, re.S)
        if m_init:
            try:
                init_json = json.loads(m_init.group(1))
                conv_bar = (
                    init_json.get("contents", {})
                    .get("twoColumnWatchNextResults", {})
                    .get("conversationBar", {})
                    .get("liveChatRenderer", {})
                )
                continuation = _extract_continuation_from_dict(conv_bar.get("continuations"))
            except Exception:
                pass

        if not continuation and vid:
            try:
                chat_url = f"https://www.youtube.com/live_chat?v={urllib.parse.quote(vid)}"
                req_chat = urllib.request.Request(chat_url, headers=YT_HEADERS, method="GET")
                with urllib.request.urlopen(req_chat, context=SSL_CONTEXT, timeout=8) as resp_chat:
                    chat_html = resp_chat.read().decode("utf-8", errors="replace")
                if not api_key:
                    mk2 = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', chat_html)
                    if mk2:
                        api_key = mk2.group(1)
                m_cinit = re.search(r'window\["ytInitialData"\]\s*=\s*(\{.+?\});\s*</script>', chat_html, re.S)
                if not m_cinit:
                    m_cinit = re.search(r'var\s+ytInitialData\s*=\s*(\{.+?\});\s*</script>', chat_html, re.S)
                if m_cinit:
                    c_json = json.loads(m_cinit.group(1))
                    continuation = _extract_continuation_from_dict(c_json)
            except Exception:
                pass

        if not api_key or not continuation:
            with self.lock:
                self.status = "offline"
                self.video_id = vid or ""
                self.continuation = None
            return False

        with self.lock:
            self.api_key = api_key
            self.video_id = vid
            self.stream_title = stream_title
            self.continuation = continuation
            self.is_first_fetch = True
            self.status = "live"
            self.error_msg = ""
        return True

    def _parse_runs(self, runs):
        fragments = []
        text_parts = []
        if not isinstance(runs, list):
            return fragments, ""

        for r in runs:
            if not isinstance(r, dict):
                continue
            if "text" in r:
                t = str(r["text"])
                fragments.append({"type": "text", "text": t})
                text_parts.append(t)
            elif "emoji" in r and isinstance(r["emoji"], dict):
                em = r["emoji"]
                is_custom = bool(em.get("isCustomEmoji"))
                thumbs = (em.get("image") or {}).get("thumbnails") or []
                shortcut = (em.get("shortcuts") or [em.get("emojiId", "emoji")])[0]
                if is_custom and thumbs:
                    img_url = thumbs[-1].get("url", "")
                    if img_url.startswith("//"):
                        img_url = "https:" + img_url
                    fragments.append({
                        "type": "emote",
                        "emoteUrl": img_url,
                        "text": shortcut
                    })
                else:
                    emoji_char = em.get("emojiId") or shortcut
                    fragments.append({"type": "text", "text": emoji_char})
                    text_parts.append(emoji_char)

        return fragments, "".join(text_parts).strip()

    def _parse_badges(self, author_badges):
        badges = []
        user_color = "#ff4e45"
        if not isinstance(author_badges, list):
            return badges, user_color

        for b in author_badges:
            renderer = (b or {}).get("liveChatAuthorBadgeRenderer") or {}
            icon_type = (renderer.get("icon") or {}).get("iconType", "")
            tooltip = renderer.get("tooltip", "")
            if icon_type == "OWNER":
                badges.append({"label": "配信者", "cls": "badge-broadcaster"})
                user_color = "#facc15"
            elif icon_type == "MODERATOR":
                badges.append({"label": "MOD", "cls": "badge-mod"})
                user_color = "#38bdf8"
            elif icon_type == "VERIFIED":
                badges.append({"label": "認証", "cls": "badge-vip"})
            elif "customThumbnail" in renderer or "メンバー" in tooltip or "Member" in tooltip:
                badges.append({"label": "MEMBER", "cls": "badge-sub"})
                if user_color == "#ff4e45":
                    user_color = "#4ade80"

        return badges, user_color

    def _poll_once(self):
        with self.lock:
            api_key = self.api_key
            continuation = self.continuation
            first_fetch = self.is_first_fetch

        if not api_key or not continuation:
            return False

        url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={urllib.parse.quote(api_key)}"
        body = {
            "context": {
                "client": {
                    "hl": "ja",
                    "gl": "JP",
                    "clientName": "WEB",
                    "clientVersion": "2.20250101.00.00"
                }
            },
            "continuation": continuation
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={**YT_HEADERS, "Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            with self.lock:
                self.continuation = None
                self.status = "offline"
            return False

        live_chat_cont = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
        next_cont = _extract_continuation_from_dict(live_chat_cont.get("continuations"))
        if not next_cont:
            with self.lock:
                self.continuation = None
                self.status = "offline"
            return False

        with self.lock:
            self.continuation = next_cont
            self.is_first_fetch = False
            self.status = "live"

        actions = live_chat_cont.get("actions") or []
        self._ingest_actions(actions, first_fetch=first_fetch)
        return True

    def _worker_loop(self):
        while True:
            try:
                now = time.time()
                with self.lock:
                    active_client = (now - self.last_client_poll_ts) <= 30.0
                    has_cont = bool(self.continuation)
                    last_attempt = self.last_connect_attempt_ts

                if not active_client:
                    time.sleep(2.0)
                    continue

                config = load_twitch_config()
                vs = config.get("viewer_settings") or {}
                if vs.get("youtube_enabled") is False:
                    with self.lock:
                        self.status = "disabled"
                        self.continuation = None
                    time.sleep(3.0)
                    continue

                target_key = f"{config.get('youtube_handle', '')}|{config.get('youtube_video_id', '')}"
                if target_key != self.current_target_key:
                    self.current_target_key = target_key
                    has_cont = False
                    with self.lock:
                        self.continuation = None

                if not has_cont:
                    if (now - last_attempt) < 15.0:
                        time.sleep(1.5)
                        continue
                    with self.lock:
                        self.last_connect_attempt_ts = now
                        if self.status == "idle":
                            self.status = "connecting"
                    connected = self._connect_live_chat(config)
                    if connected:
                        self._poll_once()
                    time.sleep(2.0)
                else:
                    self._poll_once()
                    time.sleep(2.0)
            except Exception as e:
                print(f"YouTube worker error: {e}")
                time.sleep(5.0)


YOUTUBE_MANAGER = YouTubeLiveChatManager()
