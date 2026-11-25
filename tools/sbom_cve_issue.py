"""Parse Trivy SBOM JSON reports and create or update CVE alert GitHub Issues."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence


ISSUE_TITLE = "[cve-alert] SBOM に HIGH/CRITICAL の脆弱性が検出されました"
ALERT_LABEL = "cve-alert"
BRIEFING_LABEL = "status: briefing"

_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
}


class InvalidReportError(Exception):
    """Raised when the Trivy JSON report is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class VulnerabilityFinding:
    cve_id: str
    severity: str
    pkg_name: str
    installed_version: str
    fixed_version: str


def _escape_md_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _as_nonempty_str(value: object, default: str) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    if value is None:
        return default
    return str(value).strip() or default


def parse_trivy_report(report_path: Path) -> tuple[list[VulnerabilityFinding], str | None]:
    """Parse a Trivy JSON report and return sorted findings and optional CreatedAt."""
    try:
        raw_text = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidReportError(f"Failed to read Trivy report {report_path}: {exc}") from exc

    if not raw_text.strip():
        raise InvalidReportError(f"Trivy report {report_path} is empty.")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidReportError(f"Invalid JSON in Trivy report {report_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidReportError("Trivy report root must be a JSON object.")

    if "Results" not in data and "SchemaVersion" not in data:
        raise InvalidReportError("Trivy report is missing expected 'Results' or 'SchemaVersion' keys.")

    created_at_raw = data.get("CreatedAt")
    created_at = created_at_raw.strip() if isinstance(created_at_raw, str) and created_at_raw.strip() else None

    results = data.get("Results")
    if results is None:
        return [], created_at

    if not isinstance(results, list):
        raise InvalidReportError("Trivy report 'Results' field must be a list or null.")

    findings: list[VulnerabilityFinding] = []
    for result in results:
        if not isinstance(result, dict):
            raise InvalidReportError("Each entry in Trivy report 'Results' must be an object.")

        vulns = result.get("Vulnerabilities")
        if vulns is None:
            continue
        if not isinstance(vulns, list):
            raise InvalidReportError("Trivy report 'Vulnerabilities' field must be a list or null.")

        for vuln in vulns:
            if not isinstance(vuln, dict):
                raise InvalidReportError("Each entry in 'Vulnerabilities' must be an object.")

            cve_id = _as_nonempty_str(vuln.get("VulnerabilityID"), "(unknown)")
            severity = _as_nonempty_str(vuln.get("Severity"), "UNKNOWN").upper()
            pkg_name = _as_nonempty_str(vuln.get("PkgName") or vuln.get("PkgID"), "(unknown)")
            installed_version = _as_nonempty_str(vuln.get("InstalledVersion"), "(unknown)")
            fixed_version = _as_nonempty_str(vuln.get("FixedVersion"), "なし")

            findings.append(
                VulnerabilityFinding(
                    cve_id=cve_id,
                    severity=severity,
                    pkg_name=pkg_name,
                    installed_version=installed_version,
                    fixed_version=fixed_version,
                )
            )

    findings.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            f.cve_id,
            f.pkg_name,
            f.installed_version,
        )
    )
    return findings, created_at


def build_workflow_run_url(env: Mapping[str, str]) -> str | None:
    server_url = env.get("GITHUB_SERVER_URL", "").strip().rstrip("/")
    repo = env.get("GITHUB_REPOSITORY", "").strip()
    run_id = env.get("GITHUB_RUN_ID", "").strip()
    if server_url and repo and run_id:
        return f"{server_url}/{repo}/actions/runs/{run_id}"
    return None


def format_issue_body(
    findings: Sequence[VulnerabilityFinding],
    detected_at: str,
    run_url: str | None,
) -> str:
    lines = [
        "## SBOM 脆弱性検出レポート",
        "",
        f"- **検出日時**: {detected_at}",
    ]
    if run_url:
        lines.append(f"- **ワークフロー実行**: {run_url}")
    lines.extend(
        [
            f"- **検出件数**: {len(findings)} 件",
            "",
            "| CVE ID | 深刻度 | コンポーネント | 検出バージョン | 修正バージョン |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for finding in findings:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_md_cell(finding.cve_id),
                    _escape_md_cell(finding.severity),
                    _escape_md_cell(finding.pkg_name),
                    _escape_md_cell(finding.installed_version),
                    _escape_md_cell(finding.fixed_version),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _run_gh(
    args: list[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    proc = runner(
        ["gh", *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return proc.stdout.strip()


def find_open_alert_issue(
    repo: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int | None:
    raw = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            ALERT_LABEL,
            "--state",
            "open",
            "--limit",
            "10",
            "--json",
            "number,createdAt",
        ],
        runner=runner,
    )
    issues = json.loads(raw) if raw else []
    if not isinstance(issues, list) or not issues:
        return None

    valid_issues = [item for item in issues if isinstance(item, dict) and isinstance(item.get("number"), int)]
    if not valid_issues:
        return None
    valid_issues.sort(key=lambda item: (str(item.get("createdAt", "")), item["number"]))
    return valid_issues[0]["number"]


def create_alert_issue(
    repo: str,
    body: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)

    try:
        return _run_gh(
            [
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                ISSUE_TITLE,
                "--label",
                ALERT_LABEL,
                "--label",
                BRIEFING_LABEL,
                "--body-file",
                str(tmp_path),
            ],
            runner=runner,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def comment_alert_issue(
    repo: str,
    issue_number: int,
    body: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)

    try:
        return _run_gh(
            [
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo,
                "--body-file",
                str(tmp_path),
            ],
            runner=runner,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(
        description="Parse Trivy SBOM JSON output and create/update a CVE alert GitHub Issue."
    )
    parser.add_argument(
        "--report",
        required=True,
        type=Path,
        help="Path to Trivy JSON report file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print formatted Issue body without calling gh.",
    )
    args = parser.parse_args(argv)
    environ = os.environ if env is None else env

    try:
        findings, created_at = parse_trivy_report(args.report)
    except InvalidReportError as exc:
        print(
            f"ERROR: {exc} (treating as scan/report failure; no Issue will be created).",
            file=sys.stderr,
        )
        return 2

    if not findings:
        print("No HIGH/CRITICAL vulnerabilities found in Trivy JSON report. No Issue created.")
        return 0

    detected_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run_url = build_workflow_run_url(environ)
    body = format_issue_body(findings, detected_at=detected_at, run_url=run_url)

    if args.dry_run:
        print(body)
        return 0

    repo = environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        print("ERROR: GITHUB_REPOSITORY environment variable is required.", file=sys.stderr)
        return 2

    existing_issue = find_open_alert_issue(repo, runner=runner)
    if existing_issue is not None:
        result = comment_alert_issue(repo, existing_issue, body, runner=runner)
        print(f"Commented on existing CVE alert Issue #{existing_issue}: {result}")
    else:
        result = create_alert_issue(repo, body, runner=runner)
        print(f"Created new CVE alert Issue: {result}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
