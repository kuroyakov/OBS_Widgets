"""
VOICEVOX Engine 連携モジュール (server/voicevox.py)
- ローカル VOICEVOX Engine (デフォルト http://127.0.0.1:50021) の話者・スタイル一覧取得
- audio_query 生成 ＆ WAV バイナリ音声合成プロキシ
"""

import ipaddress
import json
import os
import urllib.request
import urllib.parse

from .config import load_twitch_config

DEFAULT_VOICEVOX_URL = "http://127.0.0.1:50021"
ENV_ALLOW_PRIVATE_VOICEVOX_URL = "OBS_WIDGETS_ALLOW_PRIVATE_VOICEVOX_URL"


def validate_voicevox_url(url, allow_private=None):
    """VOICEVOX ベース URL を検証し、正規化した URL を返す (Issue #49)。

    - スキームは http / https のみ
    - ホストはループバック (127.0.0.1 / localhost / ::1) を既定とし、
      プライベート IP は OBS_WIDGETS_ALLOW_PRIVATE_VOICEVOX_URL=1 の明示 opt-in 時のみ許可
    - リンクローカル (169.254.x.x / fe80::)・公開インターネット・file://・ftp:// 等は常に拒否
    - パス・クエリ・フラグメント・ユーザー情報付き URL は拒否
    """
    if not isinstance(url, str):
        raise ValueError("voicevox_url は文字列で指定してください")
    raw = url.strip()
    if not raw:
        raise ValueError("voicevox_url が空です")

    parsed = urllib.parse.urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"voicevox_url のスキームは http/https のみ許可されます: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("voicevox_url に認証情報は指定できません")
    if parsed.query or parsed.fragment:
        raise ValueError("voicevox_url にクエリ・フラグメントは指定できません")
    if parsed.path not in ("", "/"):
        raise ValueError(f"voicevox_url にパスは指定できません: {parsed.path!r}")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"voicevox_url のポートが不正です: {exc}") from exc

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("voicevox_url にホスト名がありません")

    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"voicevox_url に許可されていないホストです: {host!r}") from exc
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"voicevox_url に許可されていないアドレスです: {host!r}")
        if not ip.is_loopback:
            if allow_private is None:
                allow_private = os.environ.get(ENV_ALLOW_PRIVATE_VOICEVOX_URL, "").strip() == "1"
            if not (allow_private and ip.is_private):
                raise ValueError(f"voicevox_url に許可されていないホストです: {host!r}")

    return raw.rstrip("/")


def get_voicevox_base_url(config=None):
    if config is None:
        config = load_twitch_config()
    vs = config.get("viewer_settings") or {}
    raw_url = vs.get("voicevox_url")
    if raw_url is None or (isinstance(raw_url, str) and not raw_url.strip()):
        return DEFAULT_VOICEVOX_URL
    return validate_voicevox_url(raw_url)


def fetch_voicevox_speakers(config=None):
    try:
        base_url = get_voicevox_base_url(config)
    except ValueError as e:
        vs = (config or {}).get("viewer_settings") or {}
        raw_url = str(vs.get("voicevox_url") or "")
        return {
            "running": False,
            "url": raw_url,
            "speakers": [],
            "error": f"VOICEVOX URL が不正です: {e}"
        }, 200
    req = urllib.request.Request(f"{base_url}/speakers", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            speakers_raw = json.loads(resp.read().decode("utf-8"))
            styles = []
            for sp in speakers_raw:
                sp_name = sp.get("name", "Unknown")
                for st in sp.get("styles", []):
                    st_id = st.get("id")
                    st_name = st.get("name", "ノーマル")
                    styles.append({
                        "id": st_id,
                        "name": f"{sp_name} ({st_name})",
                        "speaker_name": sp_name,
                        "style_name": st_name
                    })
            return {"running": True, "url": base_url, "speakers": styles}, 200
    except Exception:
        return {
            "running": False,
            "url": base_url,
            "speakers": [],
            "error": f"VOICEVOX Engine ({base_url}) に接続できません。VOICEVOXアプリを起動してください。"
        }, 200


def synthesize_voicevox_wav(
    text,
    speaker_id=3,
    speed_scale=1.05,
    volume_scale=1.0,
    pitch_scale=0.0,
    intonation_scale=1.0,
    config=None
):
    base_url = get_voicevox_base_url(config)
    query_params = urllib.parse.urlencode({"text": text, "speaker": int(speaker_id)})
    query_url = f"{base_url}/audio_query?{query_params}"

    # 1. audio_query 生成
    req_q = urllib.request.Request(query_url, data=b"", method="POST")
    with urllib.request.urlopen(req_q, timeout=6) as resp_q:
        query_json = json.loads(resp_q.read().decode("utf-8"))

    query_json["speedScale"] = float(speed_scale)
    query_json["volumeScale"] = float(volume_scale)
    query_json["pitchScale"] = float(pitch_scale)
    query_json["intonationScale"] = float(intonation_scale)

    # 2. synthesis で WAV 音声合成
    synth_params = urllib.parse.urlencode({"speaker": int(speaker_id)})
    synth_url = f"{base_url}/synthesis?{synth_params}"
    body_bytes = json.dumps(query_json).encode("utf-8")
    req_s = urllib.request.Request(
        synth_url,
        data=body_bytes,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_s, timeout=10) as resp_s:
        return resp_s.read()
