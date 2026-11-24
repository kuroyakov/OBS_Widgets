# Security Policy

## Supported Versions

セキュリティ修正は、原則として最新の `main` ブランチおよび各ウィジェットの最新リリースに対して提供されます。

| Target | Supported |
| --- | --- |
| `main` (latest) / latest widget releases | Yes |
| Older releases | No |

## Reporting a Vulnerability

セキュリティ上の脆弱性を発見した場合は、**公開 Issue（Public Issue）には投稿せず**、GitHub リポジトリの **Security** タブにある **Report a vulnerability**（Private Vulnerability Reporting）からご報告ください。

### 報告時のご注意（重要）

* **認証情報・個人情報を公開しない**: 公開 Issue、ディスカッション、スクリーンショット、ログ貼り付け欄などに、以下のような秘密情報・個人情報を絶対に掲載しないでください。
  * `twitch_config.json` / `voice_command_config.json` / `shared/env-config.local.js` の内容
  * `api_token`、`obs_password`、Twitch の `access_token` / `refresh_token` / `client_secret`
  * ご自身のローカル環境情報（ユーザー名を含むホームディレクトリパス、LAN 内 IP アドレス、マイク・デバイス名など）
* 再現手順、影響範囲、想定される攻撃経路を可能な範囲でお知らせいただけると調査がスムーズです。
