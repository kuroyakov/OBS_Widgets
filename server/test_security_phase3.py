#!/usr/bin/env python3
"""
セキュリティ Phase 3 (Issue #53) の単体テスト:
- Twitch OAuth state の TTL 付き辞書化（連続発行保持・消費後再利用拒否・TTL 超過拒否・上限件数）
- save_twitch_config の POSIX 0o600 パーミッション設定
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from server import config as server_config  # noqa: E402
from server import handler as server_handler  # noqa: E402


class TestOAuthStatePool(unittest.TestCase):
    def setUp(self):
        with server_handler._oauth_state_lock:
            server_handler._pending_oauth_states.clear()

    def tearDown(self):
        with server_handler._oauth_state_lock:
            server_handler._pending_oauth_states.clear()

    def test_multiple_issued_states_are_all_retained_and_single_use(self):
        s1 = server_handler._issue_oauth_state()
        s2 = server_handler._issue_oauth_state()
        self.assertNotEqual(s1, s2)

        # 連続発行しても先の state が失われない
        self.assertTrue(server_handler._consume_oauth_state(s1))
        # 消費後の再利用は拒否される
        self.assertFalse(server_handler._consume_oauth_state(s1))

        # 2つ目も有効で、消費後は再利用できない
        self.assertTrue(server_handler._consume_oauth_state(s2))
        self.assertFalse(server_handler._consume_oauth_state(s2))

    def test_expired_state_is_rejected(self):
        base_time = 1_700_000_000.0
        with mock.patch.object(server_handler.time, "time", return_value=base_time):
            s = server_handler._issue_oauth_state()

        with mock.patch.object(
            server_handler.time,
            "time",
            return_value=base_time + server_handler._OAUTH_STATE_TTL + 1.0,
        ):
            self.assertFalse(server_handler._consume_oauth_state(s))

    def test_max_entries_evicts_oldest_and_keeps_latest(self):
        states = []
        base_time = 1_700_000_000.0
        total = server_handler._OAUTH_STATE_MAX_ENTRIES + 5
        for i in range(total):
            with mock.patch.object(server_handler.time, "time", return_value=base_time + i):
                states.append(server_handler._issue_oauth_state())

        with mock.patch.object(server_handler.time, "time", return_value=base_time + total):
            # 最も古いものは押し出されている
            self.assertFalse(server_handler._consume_oauth_state(states[0]))
            # 最新のものは保持されている
            self.assertTrue(server_handler._consume_oauth_state(states[-1]))


class TestTwitchConfigFilePermission(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permission test only")
    def test_save_twitch_config_sets_0600_on_posix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "twitch_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"channel_name": "existing_channel"}, f)
            os.chmod(cfg_path, 0o644)

            with mock.patch.object(server_config, "CONFIG_FILE", cfg_path):
                server_config._CONFIG_CACHE["key"] = None
                server_config._CONFIG_CACHE["value"] = None
                try:
                    server_config.save_twitch_config({"current_ticker": "hello"})
                    mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
                    self.assertEqual(mode, 0o600)
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    self.assertEqual(saved["channel_name"], "existing_channel")
                    self.assertEqual(saved["current_ticker"], "hello")
                finally:
                    server_config._CONFIG_CACHE["key"] = None
                    server_config._CONFIG_CACHE["value"] = None


if __name__ == "__main__":
    unittest.main()
