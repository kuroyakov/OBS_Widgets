"""
Twitch Helix API ＆ OAuth 連携モジュール (server/twitch_api.py)
- アクセストークンの有効性・スコープ検証 (validate_twitch_token)
- リフレッシュトークンによる自動更新 (refresh_twitch_token)
- Twitch Helix API 汎用リクエスト (twitch_request)
- broadcaster_id 解決・EventSub WebSocket サブスクリプション一括登録
"""

import json
import urllib.request
import urllib.error
import urllib.parse

from .config import (
    SSL_CONTEXT,
    REQUIRED_SCOPES,
    load_twitch_config,
    save_twitch_config
)


def validate_twitch_token(config=None):
    """現在のアクセストークンの有効性と付与されているスコープを確認"""
    if config is None:
        config = load_twitch_config()
    access_token = config.get("access_token", "").strip().replace("oauth:", "")
    if not access_token:
        return {"valid": False, "scopes": [], "missing_scopes": REQUIRED_SCOPES}

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {access_token}"},
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            scopes = data.get("scopes") or []
            missing = [s for s in REQUIRED_SCOPES if s not in scopes]
            return {
                "valid": True,
                "client_id": data.get("client_id", ""),
                "login": data.get("login", ""),
                "user_id": data.get("user_id", ""),
                "scopes": scopes,
                "missing_scopes": missing,
                "expires_in": data.get("expires_in", 0)
            }
    except urllib.error.HTTPError as e:
        if e.code == 401 and config.get("refresh_token"):
            if refresh_twitch_token(config):
                return validate_twitch_token(load_twitch_config())
        return {"valid": False, "scopes": [], "missing_scopes": REQUIRED_SCOPES}
    except Exception as e:
        return {"valid": False, "error": str(e), "scopes": [], "missing_scopes": REQUIRED_SCOPES}


def refresh_twitch_token(config=None):
    """リフレッシュトークンを使って新しいアクセストークンを取得"""
    if config is None:
        config = load_twitch_config()

    client_id = config.get("client_id", "").strip()
    client_secret = config.get("client_secret", "").strip()
    refresh_token = config.get("refresh_token", "").strip()

    if not client_id or not refresh_token:
        return False

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id
    }
    if client_secret:
        params["client_secret"] = client_secret

    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            new_access = res_json.get("access_token")
            new_refresh = res_json.get("refresh_token", refresh_token)
            if new_access:
                save_twitch_config({
                    "access_token": new_access,
                    "refresh_token": new_refresh
                })
                print("✓ Twitch Access Token を自動リフレッシュしました！")
                return True
    except Exception as e:
        print(f"Token refresh failed: {e}")
    return False


def twitch_request(endpoint, method="GET", body=None, config=None, retry_on_auth_fail=True):
    """Twitch Helix API を呼び出すヘルパー（SSL証明書回避 ＆ 401自動リフレッシュ）"""
    if config is None:
        config = load_twitch_config()

    client_id = config.get("client_id", "").strip()
    access_token = config.get("access_token", "").strip().replace("oauth:", "")

    if not client_id or not access_token:
        return {"error": "TwitchのClient IDまたはAccess Tokenが未設定です。"}, 400

    url = f"https://api.twitch.tv/helix/{endpoint}"
    headers = {
        "Client-Id": client_id,
        "Authorization": f"Bearer {access_token}"
    }

    data_bytes = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=8) as response:
            if response.status == 204:
                return {"status": "success"}, 200
            res_body = response.read().decode("utf-8")
            return json.loads(res_body) if res_body else {"status": "success"}, response.status
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry_on_auth_fail and config.get("refresh_token"):
            print("Access Token expired. Trying refresh...")
            if refresh_twitch_token(config):
                return twitch_request(endpoint, method, body, config=None, retry_on_auth_fail=False)

        err_msg = e.read().decode("utf-8") if e.fp else str(e)
        print(f"Twitch API HTTPError ({e.code}): {err_msg}")
        try:
            err_json = json.loads(err_msg)
            return {"error": err_json.get("message", err_msg), "code": e.code}, e.code
        except Exception:
            return {"error": err_msg, "code": e.code}, e.code
    except Exception as e:
        print(f"Twitch API Request Error: {e}")
        return {"error": str(e)}, 500


def get_broadcaster_id_by_login(login_name, config):
    """ユーザー名から Twitch の broadcaster_id を取得"""
    data, code = twitch_request(f"users?login={urllib.parse.quote(login_name)}", "GET", config=config)
    if code == 200 and data.get("data") and len(data["data"]) > 0:
        return data["data"][0]["id"]
    return None


def register_eventsub_subscriptions(session_id, b_id, user_id, config):
    """EventSub WebSocket 用の各種サブスクリプションを一括登録"""
    subscriptions_to_create = [
        {
            "name": "チャットメッセージ",
            "type": "channel.chat.message",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id), "user_id": str(user_id)}
        },
        {
            "name": "新規フォロー",
            "type": "channel.follow",
            "version": "2",
            "condition": {"broadcaster_user_id": str(b_id), "moderator_user_id": str(user_id)}
        },
        {
            "name": "チャンネルポイント交換",
            "type": "channel.channel_points_custom_reward_redemption.add",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "新規サブスク",
            "type": "channel.subscribe",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "再サブスクメッセージ",
            "type": "channel.subscription.message",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "サブスクギフト",
            "type": "channel.subscription.gift",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "ビッツ (Cheer)",
            "type": "channel.cheer",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "レイド (Raid)",
            "type": "channel.raid",
            "version": "1",
            "condition": {"to_broadcaster_user_id": str(b_id)}
        },
        {
            "name": "ハイプトレイン開始 (UC-09 履歴記録用)",
            "type": "channel.hype_train.begin",
            "version": "2",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "ハイプトレイン進行 (UC-09 履歴記録用)",
            "type": "channel.hype_train.progress",
            "version": "2",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "ハイプトレイン終了 (UC-09 履歴記録用)",
            "type": "channel.hype_train.end",
            "version": "2",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "配信開始 (UC-09 履歴記録用)",
            "type": "stream.online",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        },
        {
            "name": "配信終了 (UC-09 履歴記録用)",
            "type": "stream.offline",
            "version": "1",
            "condition": {"broadcaster_user_id": str(b_id)}
        }
    ]

    results = []
    for sub in subscriptions_to_create:
        payload = {
            "type": sub["type"],
            "version": sub["version"],
            "condition": sub["condition"],
            "transport": {
                "method": "websocket",
                "session_id": session_id
            }
        }
        res_data, code = twitch_request("eventsub/subscriptions", method="POST", body=payload, config=config)
        results.append({
            "name": sub["name"],
            "type": sub["type"],
            "success": code in (200, 202),
            "code": code,
            "error": res_data.get("error") if code not in (200, 202) else None
        })
    return results
