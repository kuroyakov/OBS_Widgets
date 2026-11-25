"""
外国語コメント翻訳 ＆ 音声認識字幕ちゃん (jimakuChan) 連動モジュール (server/translator.py)
- 日本語判定・翻訳結果の妥当性検証・インメモリ翻訳キャッシュ
- Google Translate (GTX) ＆ MyMemory API による日本語翻訳（429クールダウン制御付き）
- 海外コメント検出時の字幕ちゃん翻訳言語切り替え（5分無発言で英語へ自動復帰）
"""

import re
import time
import json
import urllib.request
import urllib.error
import urllib.parse
from collections import OrderedDict

from .config import SSL_CONTEXT

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

JP_CHAR_REGEX = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\uFF66-\uFF9D]")  # ひらがな・カタカナ（半角カタカナ含む）
HAS_LETTER_REGEX = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF\uAC00-\uD7AF\u4E00-\u9FFF]")

# 翻訳キャッシュ（最大300件）と GTX 429 レート制限クールダウン管理
_TRANSLATION_CACHE = OrderedDict()
_TRANSLATION_CACHE_MAX = 300
_GTX_COOLDOWN_UNTIL = 0.0


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


def infer_foreign_lang(text):
    """GTXが429等で言語コードを返せなかった場合の文字種・特徴語ベースの言語推定"""
    if not text:
        return "en"
    if re.search(r"[\uAC00-\uD7AF\u1100-\u11FF]", text):
        return "ko"
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    if re.search(r"[\u0E00-\u0E7F]", text):
        return "th"
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    if re.search(r"[\u4E00-\u9FFF]", text) and not JP_CHAR_REGEX.search(text):
        return "zh-CN"
    low = f" {text.lower()} "
    if re.search(r"[àâçéèêëîïôùûüœ]", low) or any(w in low for w in (" tu ", " vous ", " suis ", " est ", " combien ", " merci ", " bonjour ")):
        return "fr"
    if re.search(r"[ñ¿¡]", low) or any(w in low for w in (" hola ", " gracias ", " cómo ", " estas ", " muy ")):
        return "es"
    if re.search(r"[äöüß]", low) or any(w in low for w in (" ich ", " nicht ", " danke ", " gut ")):
        return "de"
    if re.search(r"[ãõ]", low) or any(w in low for w in (" obrigado ", " muito ", " bom ")):
        return "pt"
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


def is_probably_japanese(text):
    """ひらがな・カタカナが含まれていれば日本語と判定。また英数字・記号のみも翻訳不要"""
    if not text:
        return True
    if JP_CHAR_REGEX.search(text):
        return True
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = re.sub(r"[0-9\s!-/:-@\[-`{-~！？。、…「」『』（）【】ｗwW８8]+", "", cleaned)
    if not cleaned:
        return True
    return False


def is_valid_japanese_translation(original_text, translated_text):
    """スペースや疑問符が変わっただけの未翻訳を除外し、日本語文字が含まれているか判定"""
    if not translated_text:
        return False
    strip_re = re.compile(r"[\s!-/:-@\[-`{-~！？。、…「」『』（）【】・ー]+")
    if strip_re.sub("", original_text.lower()) == strip_re.sub("", translated_text.lower()):
        return False
    return bool(re.search(r"[\u3041-\u3096\u30A1-\u30FA\u30FD-\u30FF\u4E00-\u9FFF]", translated_text))


def _save_cache(raw_text, result_dict):
    _TRANSLATION_CACHE[raw_text] = result_dict
    _TRANSLATION_CACHE.move_to_end(raw_text)
    while len(_TRANSLATION_CACHE) > _TRANSLATION_CACHE_MAX:
        _TRANSLATION_CACHE.popitem(last=False)


def translate_to_japanese(text):
    """外国語テキストを日本語へ翻訳する。すでに日本語の場合や記号のみはそのまま返す"""
    global _GTX_COOLDOWN_UNTIL
    raw = (text or "").strip()
    if not raw or is_probably_japanese(raw):
        return {
            "original_text": raw,
            "translated_text": raw,
            "detected_lang": "ja",
            "is_translated": False
        }

    # 0. キャッシュヒット時は外部APIを叩かず即返却（429防止）
    if raw in _TRANSLATION_CACHE:
        _TRANSLATION_CACHE.move_to_end(raw)
        return _TRANSLATION_CACHE[raw]

    detected_source = infer_foreign_lang(raw)
    now = time.time()

    # 1. Google Translate GTX エンドポイント（1回のみ試行、429発生時は60秒間クールダウンしてスキップ）
    if now >= _GTX_COOLDOWN_UNTIL:
        url = (
            "https://translate.googleapis.com/translate_a/single?"
            f"client=gtx&sl=auto&tl=ja&dt=t&dt=bd&q={urllib.parse.quote(raw)}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            },
            method="GET"
        )
        try:
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                chunks = data[0] if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list) else []
                translated = "".join([c[0] for c in chunks if isinstance(c, list) and len(c) > 0 and c[0]]).strip()
                detected_lang = data[2] if isinstance(data, list) and len(data) > 2 and isinstance(data[2], str) else ""
                if detected_lang and detected_lang != "auto":
                    detected_source = detected_lang

                if not is_valid_japanese_translation(raw, translated) and isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
                    try:
                        translated = data[1][0][1][0]
                    except Exception:
                        pass

                if is_valid_japanese_translation(raw, translated):
                    res = {
                        "original_text": raw,
                        "translated_text": translated,
                        "detected_lang": detected_source,
                        "is_translated": True
                    }
                    _save_cache(raw, res)
                    return res
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 429 Too Many Requests の場合は60秒間GTXをクールダウンし、MyMemoryへ静かにフォールバック
                _GTX_COOLDOWN_UNTIL = time.time() + 60.0
            else:
                print(f"Translate GTX HTTP error ({e.code}): {e}")
        except Exception as e:
            print(f"Translate GTX error: {e}")

    # 2. MyMemory 翻訳API フォールバック（GTXが429クールダウン中も即座にこちらで処理）
    try:
        lang_pair = f"{detected_source if detected_source != 'auto' else 'en'}|ja"
        mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(raw)}&langpair={urllib.parse.quote(lang_pair)}"
        req_mm = urllib.request.Request(
            mm_url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            method="GET"
        )
        with urllib.request.urlopen(req_mm, context=SSL_CONTEXT, timeout=4) as resp_mm:
            mm_data = json.loads(resp_mm.read().decode("utf-8"))
            mm_text = (mm_data.get("responseData") or {}).get("translatedText", "").strip()
            if is_valid_japanese_translation(raw, mm_text):
                res = {
                    "original_text": raw,
                    "translated_text": mm_text,
                    "detected_lang": detected_source,
                    "is_translated": True
                }
                _save_cache(raw, res)
                return res
    except Exception as e:
        print(f"Translate MyMemory error: {e}")

    return {
        "original_text": raw,
        "translated_text": raw,
        "detected_lang": detected_source,
        "is_translated": False
    }
