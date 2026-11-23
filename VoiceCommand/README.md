# `VoiceCommand/` — 音声認識字幕ちゃん連動 OBS 音声コマンド

音声認識字幕ちゃん（`jimakuChan-auto-lang`）の確定発話を受け取り、OBS Studio の音量調整・ミュート・フィルタ切替・ホットキー実行・離席中モード切替を行うための設定 UI とバックエンド連携です。

## セットアップ（配布パッケージ利用時）

1. **OBS WebSocket の有効化とパスワード設定**:
   - OBS Studio の「ツール → WebSocket サーバー設定」で「WebSocket サーバーを有効にする」を ON にし、ポート（既定 `4455`）とパスワードを設定します。
   - パッケージ直下の `shared/env-config.example.local.js` を `shared/env-config.local.js` にコピーし、`OBS_DEFAULT_PASSWORD` に同じパスワードを記入します。
2. **同梱 Chrome 拡張機能（`jimakuChan-auto-lang/`）の導入**:
   - Chrome で `chrome://extensions` を開き、右上の「デベロッパーモード」を ON にします。
   - 「パッケージ化されていない拡張機能を読み込む」をクリックし、パッケージ内の `jimakuChan-auto-lang/` フォルダを選択します。
3. **ローカルサーバーの起動**:
   - パッケージ直下で `python3 server.py` を起動します。
4. **音声コマンドの設定・テスト実行**:
   - Chrome で `http://localhost:8080/VoiceCommand/settings.html` を開きます。
   - ウェイクワード（既定 `コマンド`）、音量・ミュート対象、カスタムコマンド（OBS フィルタ / OBS ホットキー / 離席中モード）を設定して保存し、画面内のテスト実行ボタンで OBS が反応することを確認します（`voice_command_config.json` に保存され、サーバー再起動なしで即反映されます）。
5. **音声認識字幕ちゃんでの発話テスト**:
   - Chrome で「音声認識字幕ちゃん（`https://sayonari.github.io/jimakuChan/v2/`）」を開き、音声認識を開始して「コマンド、ゲーム音上げて」のように発話します。OBS が操作され、字幕ちゃん画面右下に実行結果トーストが表示されれば完了です。

## 使い方

1. `python3 server.py` を起動します。
2. ブラウザで `http://localhost:8080/VoiceCommand/settings.html` を開きます。
3. ウェイクワード（既定 `コマンド`）、音量・ミュート対象、カスタムコマンド（OBS フィルタ / OBS ホットキー / 離席中モード）、字幕ソース連動（対象マイクのミュートに連動して字幕ソースを非表示・解除時に連動が隠したもののみ復帰）を設定して保存します（`voice_command_config.json` に保存され、サーバー再起動なしで即反映されます）。
4. Chrome で「音声認識字幕ちゃん」を開き、「コマンド、ゲーム音上げて」のように発話すると、OBS が操作され、字幕ちゃん画面右下に実行結果トーストが表示されます。

## API（認証必須: ループバック接続または `X-Widget-Token`）

- `GET /api/voice-command/config` / `POST /api/voice-command/config`
- `GET /api/voice-command/obs-resources`
- `GET /api/voice-command/history`
- `POST /api/voice-command/utterance`
- `POST /api/voice-command/test`
