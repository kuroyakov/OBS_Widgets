"""
音声認識字幕ちゃん (jimakuChan) 連動用・翻訳言語ステート管理モジュール (server/subtitle_lang.py)
- 海外コメント検出時の字幕ちゃん翻訳言語切り替え（5分無発言で英語へ自動復帰）
"""

import time

# 音声認識字幕ちゃん (jimakuChan v2) 連動用・翻訳言語ステート（5分無発言で英語へ自動復帰）
SUBTITLE_TIMEOUT_SEC = 300  # 5分 (300秒)
SUBTITLE_LANG_NAMES_JA = {
    "en": "英語", "ko": "韓国語", "zh-CN": "中国語", "zh-TW": "台湾語", "zh-HK": "香港語",
    "fr": "フランス語", "it": "イタリア語", "de": "ドイツ語", "es": "スペイン語",
    "pt": "ポルトガル語", "ru": "ロシア語", "tr": "トルコ語", "sv": "スウェーデン語",
    "pl": "ポーランド語", "uk": "ウクライナ語", "nl": "オランダ語", "id": "インドネシア語",
    "vi": "ベトナム語", "th": "タイ語", "ar": "アラビア語", "hi": "ヒンディー語", "so": "ソマリ語", "el": "ギリシャ語"
}
SUBTITLE_LANG_STATE = {
    "active_lang": "en",
    "last_foreign_comment_ts": 0.0
}


def normalize_subtitle_lang(raw_lang):
    if not raw_lang:
        return "en"
    s = str(raw_lang).strip()
    low = s.lower()
    if low in ("ja", "jp", "auto", "unknown", "error", "foreign"):
        return "en"
    if low.startswith("zh-tw") or low.startswith("zh-hant"):
        return "zh-TW"
    if low.startswith("zh-hk"):
        return "zh-HK"
    if low.startswith("zh"):
        return "zh-CN"
    base = low.split("-")[0]
    if base in SUBTITLE_LANG_NAMES_JA:
        return base
    return "en"


def get_current_subtitle_lang_status():
    now = time.time()
    lang = SUBTITLE_LANG_STATE["active_lang"]
    last_ts = SUBTITLE_LANG_STATE["last_foreign_comment_ts"]

    if last_ts > 0:
        elapsed = now - last_ts
        if elapsed >= SUBTITLE_TIMEOUT_SEC:
            SUBTITLE_LANG_STATE["active_lang"] = "en"
            SUBTITLE_LANG_STATE["last_foreign_comment_ts"] = 0.0
            lang = "en"
            remaining = 0
        else:
            remaining = int(round(SUBTITLE_TIMEOUT_SEC - elapsed))
    else:
        remaining = 0

    return {
        "lang": lang,
        "lang_name_ja": SUBTITLE_LANG_NAMES_JA.get(lang, lang.upper()),
        "default_lang": "en",
        "is_switched": (lang != "en" and remaining > 0),
        "remaining_sec": remaining,
        "timeout_sec": SUBTITLE_TIMEOUT_SEC,
        "last_foreign_comment_ts": SUBTITLE_LANG_STATE["last_foreign_comment_ts"]
    }


def update_subtitle_lang_state(req_data):
    """POST /api/subtitle/lang のリクエスト内容に応じてステートを更新して返す"""
    if req_data.get("reset"):
        SUBTITLE_LANG_STATE["active_lang"] = "en"
        SUBTITLE_LANG_STATE["last_foreign_comment_ts"] = 0.0
    else:
        norm_lang = normalize_subtitle_lang(req_data.get("lang", "en"))
        SUBTITLE_LANG_STATE["active_lang"] = norm_lang
        SUBTITLE_LANG_STATE["last_foreign_comment_ts"] = time.time()
    return get_current_subtitle_lang_status()
