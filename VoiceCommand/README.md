# `VoiceCommand/` — 音声認識字幕ちゃん連動 OBS 音声コマンド

音声認識字幕ちゃん（`jimakuChan-auto-lang`）の確定発話を受け取り、OBS Studio の音量調整・ミュート・フィルタ切替・ホットキー実行・離席中モード切替を行うための設定 UI とバックエンド連携です。

## 使い方

1. `python3 server.py` を起動します。
2. ブラウザで `http://localhost:8080/VoiceCommand/settings.html` を開きます。
3. ウェイクワード（既定 `コマンド`）、音量・ミュート対象、カスタムコマンド（OBS フィルタ / OBS ホットキー / 離席中モード）を設定して保存します（`voice_command_config.json` に保存され、サーバー再起動なしで即反映されます）。
4. Chrome で「音声認識字幕ちゃん」を開き、「コマンド、ゲーム音上げて」のように発話すると、OBS が操作され、字幕ちゃん画面右下に実行結果トーストが表示されます。

## API（認証必須: ループバック接続または `X-Widget-Token`）

- `GET /api/voice-command/config` / `POST /api/voice-command/config`
- `GET /api/voice-command/obs-resources`
- `GET /api/voice-command/history`
- `POST /api/voice-command/utterance`
- `POST /api/voice-command/test`
