"""
VOICEVOX Engine 連携モジュール (server/voicevox.py)
- ローカル VOICEVOX Engine (デフォルト http://127.0.0.1:50021) の話者・スタイル一覧取得
- audio_query 生成 ＆ WAV バイナリ音声合成プロキシ
"""

import json
import urllib.request
import urllib.parse

from .config import load_twitch_config


def get_voicevox_base_url(config=None):
    if config is None:
        config = load_twitch_config()
    vs = config.get("viewer_settings") or {}
    url = (vs.get("voicevox_url") or "http://127.0.0.1:50021").strip().rstrip("/")
    return url


def fetch_voicevox_speakers(config=None):
    base_url = get_voicevox_base_url(config)
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
