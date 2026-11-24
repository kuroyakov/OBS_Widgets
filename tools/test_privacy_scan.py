#!/usr/bin/env python3
"""privacy_scan.py のフィクスチャテスト (Issue #53)。

注意: このファイル自身が PR 差分スキャン (git log -p) の対象になるため、
検出対象の秘密情報・個人情報リテラルは文字列連結で分割表記にしている。
"""
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import privacy_scan  # noqa: E402


class TestPrivacyScanFixtures(unittest.TestCase):
    def test_detects_secrets_tokens_and_windows_path_variants(self):
        pem_rsa = "-----BEGIN RSA " + "PRIVATE KEY-----"
        pem_ssh = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
        gh_classic = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
        gh_fine = "github_pat_" + "AbCdEfGhIjKlMnOpQrStUvWxYz"
        twitch_oauth = "oauth" + ":a1b2c3d4e5f6g7h8i9j0"
        win_single = "C:" + "\\Users" + "\\alice" + "\\secret.txt"
        win_escaped = "C:" + "\\\\Users" + "\\\\alice" + "\\\\secret.txt"
        win_slash = "C:" + "/Users" + "/alice" + "/secret.txt"
        lan_ip = "192." + "168.10.20"

        samples = [
            (pem_rsa, "private-key"),
            (pem_ssh, "private-key"),
            (gh_classic, "github-token"),
            (gh_fine, "github-token"),
            (twitch_oauth, "twitch-oauth"),
            (win_single, "home-path"),
            (win_escaped, "home-path"),
            (win_slash, "home-path"),
            (lan_ip, "lan-ip"),
        ]
        for text, expected_label in samples:
            with self.subTest(expected_label=expected_label):
                hits = privacy_scan.scan_text(text, {})
                labels = [label for _, label in hits]
                self.assertIn(expected_label, labels)

    def test_allows_safe_dummy_values_and_excluded_patterns(self):
        safe_lines = "\n".join([
            "dummy_ip = '198.51.100.10'",
            "doc_ip_1 = '192.0.2.1'",
            "doc_ip_2 = '203.0.113.42'",
            "irc_host = 'justinfan12345@justinfan12345.tmi.twitch.tv'",
            "asset = 'icon@2x.png'",
            "placeholder = 'oauth:your_token_here'",
        ])
        hits = privacy_scan.scan_text(safe_lines, {})
        self.assertEqual(hits, [])

    def test_utf16_le_and_be_bom_files_are_scanned_and_binary_is_skipped(self):
        secret_path = "C:" + "/Users" + "/alice" + "/Desktop/key.txt"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            le_file = tmp_dir / "le.txt"
            le_file.write_bytes(b"\xff\xfe" + secret_path.encode("utf-16-le"))

            be_file = tmp_dir / "be.txt"
            be_file.write_bytes(b"\xfe\xff" + secret_path.encode("utf-16-be"))

            bin_file = tmp_dir / "blob.bin"
            bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe")

            le_text = privacy_scan.read_text_for_scan(le_file)
            be_text = privacy_scan.read_text_for_scan(be_file)
            bin_text = privacy_scan.read_text_for_scan(bin_file)

            self.assertIsNotNone(le_text)
            self.assertIsNotNone(be_text)
            self.assertIsNone(bin_text)

            self.assertEqual([lbl for _, lbl in privacy_scan.scan_text(le_text, {})], ["home-path"])
            self.assertEqual([lbl for _, lbl in privacy_scan.scan_text(be_text, {})], ["home-path"])

            rc = privacy_scan.main([str(le_file)])
            self.assertEqual(rc, 1)
            rc_bin = privacy_scan.main([str(bin_file)])
            self.assertEqual(rc_bin, 0)


if __name__ == "__main__":
    unittest.main()
