/**
 * ローカル環境個別設定テンプレート (/shared/env-config.example.local.js)
 * 必要に応じて同ディレクトリに「env-config.local.js」としてコピーし、
 * 各設定値を入力してください。
 */
window.OBS_LOCAL_ENV = {
  OBS_DEFAULT_PASSWORD: 'YOUR_OBS_PASSWORD',
  TWITCH_CHANNEL: 'your_channel',
  STREAMER_NAME: 'STREAMER_NAME'
  // OBS のシーン名・ソース名・フィルタ名が既定値と異なる場合は、必要なものだけ
  // 上書きできます（例）:
  // OBS_SCENE_NAMES: { starting: '開始画面', liveMain: 'メイン', liveChat: '雑談', ending: '終了画面' },
  // OBS_MEDIA_SOURCES: { intro: 'イントロ', bgm: 'BGMソース', outro: 'アウトロ', keyboardVisualizer: 'KeyboardVisualizer' },
  // OBS_FILTER_NAMES: { bgmFadeIn: 'フェードイン', bgmFadeOut: 'フェードアウト' },
  // OBS_AUDIO_SOURCES: { desktop: 'デスクトップ音声' }
};
