// ========================================================
// VOICEVOX Engine 連携・読み上げ音声合成キュー (js/voicevox-tts.js)
// ========================================================
'use strict';

/* ========================================================
 * STEP 2: VOICEVOX 連携（話者リスト取得 ＆ 読み上げキュー）
 * ======================================================== */
const ttsQueue = [];
let isPlayingTts = false;

async function loadVoicevoxSpeakers() {
  // 1. まずローカルの VOICEVOX Engine (127.0.0.1:50021) に直接接続を試行、失敗時は server.py 経由
  let speakersList = null;
  try {
    const directRes = await fetch(`${VOICEVOX_DIRECT_URL}/speakers`);
    if (directRes.ok) {
      const raw = await directRes.json();
      speakersList = [];
      raw.forEach(sp => {
        (sp.styles || []).forEach(st => {
          speakersList.push({
            id: st.id,
            name: `${sp.name} (${st.name})`
          });
        });
      });
    }
  } catch (e) {}

  if (!speakersList) {
    try {
      const res = await fetch(`${API_BASE}/api/voicevox/speakers`);
      const data = await res.json();
      if (data.running && Array.isArray(data.speakers)) {
        speakersList = data.speakers;
      }
    } catch (e) {}
  }

  if (speakersList && speakersList.length > 0) {
    dotVoicevox.className = 'dot ok';
    dotVoicevox.title = `VOICEVOX 接続完了 (${speakersList.length}スタイル)`;
    const currentVal = String(settings.speaker_id || selSpeaker.value || '3');
    selSpeaker.innerHTML = '';
    speakersList.forEach(sp => {
      const opt = document.createElement('option');
      opt.value = String(sp.id);
      opt.textContent = sp.name;
      selSpeaker.appendChild(opt);
    });
    if (Array.from(selSpeaker.options).some(o => o.value === currentVal)) {
      selSpeaker.value = currentVal;
    }
  } else {
    dotVoicevox.className = 'dot warn';
    dotVoicevox.title = 'VOICEVOX未起動 (起動検知で自動接続します)';
  }
}

// VOICEVOX が後から起動された場合でも自動で話者リストを取得して緑ドットにする定期チェック
setInterval(() => {
  if (!dotVoicevox.classList.contains('ok')) {
    loadVoicevoxSpeakers();
  }
}, 6000);

btnReloadSpeakers.addEventListener('click', loadVoicevoxSpeakers);

function sanitizeUsernameForSpeech(name) {
  if (!name) return '';
  // アンダースコアや数字の羅列を読みやすく整形し、既知の英単語があればカタカナ化
  let n = name.replace(/_/g, ' ').replace(/\d{3,}/g, '').trim();
  n = replaceRemainingEnglishWithJapanese(n);
  return n || name;
}

function sanitizeTextForTts(rawText) {
  if (!rawText) return '';
  let t = rawText;
  // URLを「URL省略」に置換
  t = t.replace(/https?:\/\/\S+/gi, 'ユーアールエル省略');
  // 残存する英単語があればカタカナ・日本語辞書で置換
  t = replaceRemainingEnglishWithJapanese(t);
  // 連続する w や ｗ を「わら」に変換
  t = t.replace(/[wｗWＷ]{2,}/g, 'わら');
  // 連続する 8888 を「ぱちぱち」に変換
  t = t.replace(/[8８]{3,}/g, 'ぱちぱち');
  t = t.trim();
  if (t.length > 90) {
    t = t.slice(0, 90) + '、以下省略';
  }
  return t;
}

function enqueueTts(japaneseText) {
  if (!settings.tts_enabled) return;
  const cleaned = sanitizeTextForTts(japaneseText);
  if (!cleaned) return;
  ttsQueue.push(cleaned);
  processTtsQueue();
}

async function synthesizeAudioBlob(text) {
  const speakerId = settings.speaker_id || 3;
  const speedScale = settings.tts_speed || 1.05;
  const volumeScale = settings.tts_volume || 1.0;

  // 経路1: ブラウザから直接 VOICEVOX Engine (127.0.0.1:50021) を叩く
  try {
    const qRes = await fetch(
      `${VOICEVOX_DIRECT_URL}/audio_query?text=${encodeURIComponent(text)}&speaker=${speakerId}`,
      { method: 'POST' }
    );
    if (qRes.ok) {
      const queryJson = await qRes.json();
      queryJson.speedScale = Number(speedScale);
      queryJson.volumeScale = Number(volumeScale);
      const sRes = await fetch(
        `${VOICEVOX_DIRECT_URL}/synthesis?speaker=${speakerId}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(queryJson)
        }
      );
      if (sRes.ok) {
        return await sRes.blob();
      }
    }
  } catch (e) {}

  // 経路2: server.py のプロキシ (/api/voicevox/tts) を叩く
  const res = await authFetch(`${API_BASE}/api/voicevox/tts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      speaker_id: speakerId,
      speed_scale: speedScale,
      volume_scale: volumeScale
    })
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return await res.blob();
}

async function processTtsQueue() {
  if (isPlayingTts || ttsQueue.length === 0) return;
  isPlayingTts = true;
  const nextText = ttsQueue.shift();

  try {
    const blob = await synthesizeAudioBlob(nextText);
    dotVoicevox.className = 'dot ok';
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    await new Promise((resolve) => {
      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        try {
          audio.pause();
          audio.removeAttribute('src');
          audio.load();
        } catch (e) {}
        URL.revokeObjectURL(audioUrl);
        resolve();
      };
      audio.onended = cleanup;
      audio.onerror = cleanup;
      audio.play().catch(cleanup);
    });
  } catch (e) {
    dotVoicevox.className = 'dot warn';
    console.warn('VOICEVOX TTS skipped:', e);
  } finally {
    isPlayingTts = false;
    if (ttsQueue.length > 0) {
      processTtsQueue();
    }
  }
}

