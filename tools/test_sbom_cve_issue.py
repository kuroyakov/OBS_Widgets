import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import sbom_cve_issue  # noqa: E402


class TestSbomCveIssue(unittest.TestCase):
    def test_parse_with_findings_and_format_body(self):
        report = {
            "SchemaVersion": 2,
            "CreatedAt": "2026-11-25T03:00:00Z",
            "Results": [
                {
                    "Target": "sbom.cdx.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2021-23337",
                            "Severity": "HIGH",
                            "PkgName": "lodash",
                            "InstalledVersion": "4.17.20",
                            "FixedVersion": "4.17.21",
                        },
                        {
                            "VulnerabilityID": "CVE-2020-8203",
                            "Severity": "CRITICAL",
                            "PkgName": "lodash",
                            "InstalledVersion": "4.17.15",
                        },
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "trivy.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            findings, created_at = sbom_cve_issue.parse_trivy_report(report_path)
            self.assertEqual(created_at, "2026-11-25T03:00:00Z")
            self.assertEqual(len(findings), 2)
            # CRITICAL should be sorted before HIGH
            self.assertEqual(findings[0].cve_id, "CVE-2020-8203")
            self.assertEqual(findings[0].severity, "CRITICAL")
            self.assertEqual(findings[0].fixed_version, "なし")
            self.assertEqual(findings[1].cve_id, "CVE-2021-23337")
            self.assertEqual(findings[1].fixed_version, "4.17.21")

            body = sbom_cve_issue.format_issue_body(
                findings,
                detected_at=created_at,
                run_url="https://github.com/kuroyakov/OBS_Widgets/actions/runs/999",
            )
            self.assertIn("2026-11-25T03:00:00Z", body)
            self.assertIn("https://github.com/kuroyakov/OBS_Widgets/actions/runs/999", body)
            self.assertIn("| CVE-2020-8203 | CRITICAL | lodash | 4.17.15 | なし |", body)
            self.assertIn("| CVE-2021-23337 | HIGH | lodash | 4.17.20 | 4.17.21 |", body)

    def test_parse_empty_results_does_not_create_issue(self):
        empty_reports = [
            {"SchemaVersion": 2, "Results": []},
            {"SchemaVersion": 2, "Results": None},
            {"SchemaVersion": 2, "Results": [{"Target": "sbom.cdx.json"}]},
            {"SchemaVersion": 2, "Results": [{"Target": "sbom.cdx.json", "Vulnerabilities": None}]},
            {"SchemaVersion": 2, "Results": [{"Target": "sbom.cdx.json", "Vulnerabilities": []}]},
        ]
        called = False

        def fake_runner(*args, **kwargs):
            nonlocal called
            called = True
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            for idx, report in enumerate(empty_reports):
                report_path = Path(tmp) / f"empty_{idx}.json"
                report_path.write_text(json.dumps(report), encoding="utf-8")

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    rc = sbom_cve_issue.main(
                        ["--report", str(report_path)],
                        env={"GITHUB_REPOSITORY": "kuroyakov/OBS_Widgets"},
                        runner=fake_runner,
                    )
                self.assertEqual(rc, 0)
                self.assertFalse(called)
                self.assertIn("No HIGH/CRITICAL vulnerabilities", stdout.getvalue())

    def test_corrupted_or_missing_json_returns_error_and_skips_issue(self):
        called = False

        def fake_runner(*args, **kwargs):
            nonlocal called
            called = True
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            bad_cases = [
                Path(tmp) / "missing.json",
                Path(tmp) / "empty.txt",
                Path(tmp) / "broken.json",
                Path(tmp) / "list_root.json",
                Path(tmp) / "unknown_obj.json",
                Path(tmp) / "bad_results.json",
            ]
            bad_cases[1].write_text("   ", encoding="utf-8")
            bad_cases[2].write_text("{not valid json", encoding="utf-8")
            bad_cases[3].write_text("[]", encoding="utf-8")
            bad_cases[4].write_text('{"foo": "bar"}', encoding="utf-8")
            bad_cases[5].write_text('{"SchemaVersion": 2, "Results": "oops"}', encoding="utf-8")

            for path in bad_cases:
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    rc = sbom_cve_issue.main(
                        ["--report", str(path)],
                        env={"GITHUB_REPOSITORY": "kuroyakov/OBS_Widgets"},
                        runner=fake_runner,
                    )
                self.assertEqual(rc, 2)
                self.assertFalse(called)
                self.assertIn("no Issue will be created", stderr.getvalue())

    def test_creates_new_issue_when_none_open(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["gh", "issue"] and cmd[2] == "list":
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if cmd[:2] == ["gh", "issue"] and cmd[2] == "create":
                body_idx = cmd.index("--body-file") + 1
                body_text = Path(cmd[body_idx]).read_text(encoding="utf-8")
                self.assertIn("CVE-2021-23337", body_text)
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="https://github.com/kuroyakov/OBS_Widgets/issues/999\n", stderr=""
                )
            raise AssertionError(f"Unexpected command: {cmd}")

        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "sbom.cdx.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2021-23337",
                            "Severity": "HIGH",
                            "PkgName": "lodash",
                            "InstalledVersion": "4.17.20",
                            "FixedVersion": "4.17.21",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "trivy.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = sbom_cve_issue.main(
                    ["--report", str(report_path)],
                    env={
                        "GITHUB_REPOSITORY": "kuroyakov/OBS_Widgets",
                        "GITHUB_SERVER_URL": "https://github.com",
                        "GITHUB_RUN_ID": "12345",
                    },
                    runner=fake_runner,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][2], "create")
            self.assertIn(sbom_cve_issue.ISSUE_TITLE, calls[1])
            self.assertIn("cve-alert", calls[1])
            self.assertIn("status: briefing", calls[1])

    def test_comments_on_existing_open_issue(self):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["gh", "issue"] and cmd[2] == "list":
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps([{"number": 77, "createdAt": "2026-11-20T00:00:00Z"}]),
                    stderr="",
                )
            if cmd[:2] == ["gh", "issue"] and cmd[2] == "comment":
                self.assertEqual(cmd[3], "77")
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="https://github.com/kuroyakov/OBS_Widgets/issues/77#issuecomment-1\n",
                    stderr="",
                )
            raise AssertionError(f"Unexpected command: {cmd}")

        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "sbom.cdx.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2021-23337",
                            "Severity": "HIGH",
                            "PkgName": "lodash",
                            "InstalledVersion": "4.17.20",
                            "FixedVersion": "4.17.21",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "trivy.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = sbom_cve_issue.main(
                    ["--report", str(report_path)],
                    env={"GITHUB_REPOSITORY": "kuroyakov/OBS_Widgets"},
                    runner=fake_runner,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][2], "comment")


if __name__ == "__main__":
    unittest.main()
