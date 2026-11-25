#!/usr/bin/env python3
"""SBOM (CycloneDX 1.5) 生成・鮮度チェック・依存台帳検証ツール。

外部依存を増やさず Python 標準ライブラリのみで動作し、以下を検証する:
1. 外部ライブラリ (CDN / Python / CI ツール / GitHub Actions) の一覧化と sbom.cdx.json との同期
2. 外部通信先ドメイン (https:// / wss://) の抽出と sbom.cdx.json / docs/DEPENDENCIES.md への記載漏れ検知
3. セキュリティ上重要な Python 標準ライブラリモジュールの利用箇所の抽出と台帳記載漏れ検知
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SBOM_PATH = REPO_ROOT / "sbom.cdx.json"
DEFAULT_DOCS_PATH = REPO_ROOT / "docs" / "DEPENDENCIES.md"

LOCAL_PY_MODULES = {
    "server",
    "tools",
    "build_package",
    "e2e_smoke",
    "privacy_scan",
    "sbom_audit",
}

# CVE やセキュリティレビューで優先確認対象となる Python 標準ライブラリ
SECURITY_STDLIB_MODULES = {
    "http",
    "ssl",
    "urllib",
    "socket",
    "subprocess",
    "zipfile",
    "zlib",
    "tempfile",
    "shutil",
    "hashlib",
    "hmac",
    "secrets",
    "json",
    "html",
    "ipaddress",
}

IGNORED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "example.com",
    "example.org",
    "www.w3.org",
    "cyclonedx.org",
    "json-schema.org",
}

URL_PATTERN = re.compile(r"(https?|wss?)://([A-Za-z0-9.-]+)(?::\d+)?")
SCRIPT_TAG_PATTERN = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
JSDELIVR_SRC_PATTERN = re.compile(
    r'\bsrc=["\'](https://cdn\.jsdelivr\.net/npm/((?:@[^/"\']+/)?[^/@"\']+)@([^/"\']+)/[^"\']+)["\']',
    re.IGNORECASE,
)
INTEGRITY_ATTR_PATTERN = re.compile(r'\bintegrity=["\']([^"\']+)["\']', re.IGNORECASE)
ONESDK_PATTERN = re.compile(r'<script[^>]+src=["\']([^"\']*onesdk\.js)["\']')
WORKFLOW_USES_PATTERN = re.compile(
    r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})(?:\s*#\s*([^\s]+))?"
)
WORKFLOW_PIP_LINE_PATTERN = re.compile(r"pip\s+install\b[^\n]*")
PIP_PKG_VERSION_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+)")


def _list_repo_files(root_dir: Path) -> list[Path]:
    """git ls-files を使って追跡対象ファイルを列挙する（非 git 環境時は walk にフォールバック）。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root_dir),
            capture_output=True,
            check=True,
        )
        items = [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]
        return [root_dir / p for p in sorted(items) if (root_dir / p).is_file()]
    except (OSError, subprocess.CalledProcessError):
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".venv", "dist"})
            for fn in sorted(filenames):
                files.append(Path(dirpath) / fn)
        return files


def _is_test_or_tooling_url_excluded(rel_path: str) -> bool:
    """テスト用ダミー URL やメタファイル（sbom 自体など）を外部通信先スキャンから除外する。"""
    if rel_path in {
        "sbom.cdx.json",
        "tools/e2e_smoke.py",
        "tools/build_package.py",
        "tools/sbom_audit.py",
        "tools/privacy_scan_allowlist.txt",
    }:
        return True
    base = os.path.basename(rel_path)
    return base.startswith("test_")


def scan_repository(root_dir: Path = REPO_ROOT) -> dict[str, Any]:
    """リポジトリ内の外部依存・外部通信先・標準ライブラリ利用状況を走査する。"""
    files = _list_repo_files(root_dir)
    stdlib_names = sys.stdlib_module_names

    npm_cdn_libs: dict[tuple[str, str], dict[str, Any]] = {}
    local_sdks: dict[str, set[str]] = defaultdict(set)
    py_third_party: dict[str, set[str]] = defaultdict(set)
    py_security_stdlib: dict[str, set[str]] = defaultdict(set)
    external_domains: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"files": set(), "schemes": set()}
    )
    ci_actions: dict[tuple[str, str], dict[str, Any]] = {}
    ci_pip_packages: dict[tuple[str, str], set[str]] = defaultdict(set)

    for path in files:
        rel = path.relative_to(root_dir).as_posix()
        ext = path.suffix.lower()

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # 1. Python AST 解析
        if ext == ".py":
            try:
                tree = ast.parse(content, filename=rel)
            except SyntaxError:
                tree = None
            if tree is not None:
                is_test_file = os.path.basename(rel).startswith("test_")
                for node in ast.walk(tree):
                    mods: list[str] = []
                    if isinstance(node, ast.Import):
                        mods = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        if node.level == 0 and node.module:
                            mods = [node.module.split(".")[0]]
                    for mod in mods:
                        if mod in stdlib_names:
                            if not is_test_file and mod in SECURITY_STDLIB_MODULES:
                                py_security_stdlib[mod].add(rel)
                        elif mod not in LOCAL_PY_MODULES:
                            py_third_party[mod].add(rel)

        # 2. HTML 内の CDN / ローカル外部 SDK 参照
        if ext == ".html":
            for tag_match in SCRIPT_TAG_PATTERN.finditer(content):
                tag = tag_match.group(0)
                src_match = JSDELIVR_SRC_PATTERN.search(tag)
                if not src_match:
                    continue
                url, pkg_name, pkg_ver = src_match.groups()
                integrity_match = INTEGRITY_ATTR_PATTERN.search(tag)
                integrity = integrity_match.group(1) if integrity_match else ""
                key = (pkg_name, pkg_ver)
                entry = npm_cdn_libs.setdefault(
                    key,
                    {
                        "name": pkg_name,
                        "version": pkg_ver,
                        "url": url,
                        "integrity": set(),
                        "files": set(),
                    },
                )
                if integrity:
                    entry["integrity"].add(integrity)
                entry["files"].add(rel)

            for m in ONESDK_PATTERN.finditer(content):
                local_sdks[m.group(1)].add(rel)

        # 3. 外部通信先ドメイン (ランタイムコード・拡張機能)
        if ext in {".py", ".js", ".html", ".css", ".json"} and not _is_test_or_tooling_url_excluded(rel):
            for m in URL_PATTERN.finditer(content):
                scheme = m.group(1).lower()
                host = m.group(2).lower()
                if "." not in host or host in IGNORED_HOSTS:
                    continue
                external_domains[host]["files"].add(rel)
                external_domains[host]["schemes"].add(scheme)

        # 4. GitHub Actions ワークフロー
        if rel.startswith(".github/workflows/") and ext in {".yml", ".yaml"}:
            for m in WORKFLOW_USES_PATTERN.finditer(content):
                action_repo, sha, version_comment = m.groups()
                key = (action_repo, sha)
                entry = ci_actions.setdefault(
                    key,
                    {
                        "name": action_repo,
                        "sha": sha,
                        "version": version_comment or sha[:12],
                        "files": set(),
                    },
                )
                entry["files"].add(rel)

            for line_match in WORKFLOW_PIP_LINE_PATTERN.finditer(content):
                for pkg_match in PIP_PKG_VERSION_PATTERN.finditer(line_match.group(0)):
                    pkg_name, pkg_ver = pkg_match.groups()
                    ci_pip_packages[(pkg_name, pkg_ver)].add(rel)

    return {
        "npm_cdn_libs": [
            {
                **v,
                "integrity": ", ".join(sorted(v["integrity"])),
                "files": sorted(v["files"]),
            }
            for _, v in sorted(npm_cdn_libs.items())
        ],
        "local_sdks": {k: sorted(v) for k, v in sorted(local_sdks.items())},
        "py_third_party": {k: sorted(v) for k, v in sorted(py_third_party.items())},
        "py_security_stdlib": {k: sorted(v) for k, v in sorted(py_security_stdlib.items())},
        "external_domains": {
            k: {"files": sorted(v["files"]), "schemes": sorted(v["schemes"])}
            for k, v in sorted(external_domains.items())
        },
        "ci_actions": [
            {**v, "files": sorted(v["files"])}
            for _, v in sorted(ci_actions.items())
        ],
        "ci_pip_packages": [
            {"name": k[0], "version": k[1], "files": sorted(v)}
            for k, v in sorted(ci_pip_packages.items())
        ],
    }


def build_cyclonedx_sbom(scan: dict[str, Any]) -> dict[str, Any]:
    """走査結果から決定論的な CycloneDX 1.5 JSON オブジェクトを構築する。"""
    components: list[dict[str, Any]] = []

    # 1. フロントエンド CDN ライブラリ (npm)
    for lib in scan["npm_cdn_libs"]:
        comp: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"pkg:npm/{lib['name']}@{lib['version']}",
            "name": lib["name"],
            "version": lib["version"],
            "purl": f"pkg:npm/{lib['name']}@{lib['version']}",
            "scope": "required",
            "properties": [
                {"name": "obs_widgets:source", "value": lib["url"]},
                {"name": "obs_widgets:integrity", "value": lib["integrity"]},
                {"name": "obs_widgets:used_in", "value": ", ".join(lib["files"])},
            ],
        }
        components.append(comp)

    # 2. ローカル外部 SDK (未同梱)
    for sdk_path, used_files in scan["local_sdks"].items():
        components.append(
            {
                "type": "library",
                "bom-ref": f"external-local:{os.path.basename(sdk_path)}",
                "name": "OneSDK (OneComme)",
                "scope": "required",
                "description": "External local SDK provided by OneComme template directory at runtime.",
                "properties": [
                    {"name": "obs_widgets:path", "value": sdk_path},
                    {"name": "obs_widgets:used_in", "value": ", ".join(used_files)},
                ],
            }
        )

    # 3. Python ランタイム外部パッケージ (AST で検出された非標準ライブラリ)
    ci_pip_names = {p["name"] for p in scan["ci_pip_packages"]}
    for pkg, used_files in scan["py_third_party"].items():
        if pkg in ci_pip_names:
            continue
        components.append(
            {
                "type": "library",
                "bom-ref": f"pkg:pypi/{pkg}",
                "name": pkg,
                "purl": f"pkg:pypi/{pkg}",
                "scope": "optional",
                "description": "Optional runtime fallback dependency in Python server.",
                "properties": [
                    {"name": "obs_widgets:used_in", "value": ", ".join(used_files)},
                ],
            }
        )

    # 4. CI 専用 Python パッケージ
    for pkg in scan["ci_pip_packages"]:
        components.append(
            {
                "type": "library",
                "bom-ref": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
                "name": pkg["name"],
                "version": pkg["version"],
                "purl": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
                "scope": "excluded",
                "properties": [
                    {"name": "obs_widgets:scope", "value": "ci-dev"},
                    {"name": "obs_widgets:used_in", "value": ", ".join(pkg["files"])},
                ],
            }
        )

    # 5. GitHub Actions
    for act in scan["ci_actions"]:
        components.append(
            {
                "type": "application",
                "bom-ref": f"pkg:github/{act['name']}@{act['sha']}",
                "name": act["name"],
                "version": act["version"],
                "purl": f"pkg:github/{act['name']}@{act['sha']}",
                "scope": "excluded",
                "properties": [
                    {"name": "obs_widgets:scope", "value": "ci-workflow"},
                    {"name": "obs_widgets:used_in", "value": ", ".join(act["files"])},
                ],
            }
        )

    # 6. セキュリティ関連 Python 標準ライブラリ利用サーフェス
    stdlib_props = [
        {"name": f"obs_widgets:stdlib:{mod}", "value": ", ".join(files)}
        for mod, files in scan["py_security_stdlib"].items()
    ]
    components.append(
        {
            "type": "platform",
            "bom-ref": "platform:python3-stdlib",
            "name": "Python 3 Standard Library (Security-Relevant Surface)",
            "scope": "required",
            "properties": stdlib_props,
        }
    )

    # 7. 外部通信先サービス (Services)
    services = [
        {
            "bom-ref": f"service:{host}",
            "name": host,
            "endpoints": [f"{scheme}://{host}" for scheme in info["schemes"]],
            "properties": [
                {"name": "obs_widgets:used_in", "value": ", ".join(info["files"])},
            ],
        }
        for host, info in scan["external_domains"].items()
    ]

    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "OBS_Widgets",
                "name": "OBS_Widgets",
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "tools": [
                {
                    "name": "tools/sbom_audit.py",
                }
            ],
        },
        "components": components,
        "services": services,
    }


def check_drift(
    root_dir: Path = REPO_ROOT,
    sbom_path: Path = DEFAULT_SBOM_PATH,
    docs_path: Path = DEFAULT_DOCS_PATH,
) -> list[str]:
    """SBOM ファイルと依存台帳ドキュメントが最新コードと一致しているか検証し、問題リストを返す。"""
    errors: list[str] = []
    scan = scan_repository(root_dir)
    expected_sbom = build_cyclonedx_sbom(scan)
    expected_text = json.dumps(expected_sbom, indent=2, ensure_ascii=False) + "\n"

    if not sbom_path.is_file():
        errors.append(
            f"SBOM file missing: {sbom_path}. Run `python3 tools/sbom_audit.py --generate-sbom`."
        )
    else:
        actual_text = sbom_path.read_text(encoding="utf-8")
        if actual_text != expected_text:
            errors.append(
                f"SBOM drift detected in {sbom_path.name}. "
                "Run `python3 tools/sbom_audit.py --generate-sbom` to update it."
            )

    if not docs_path.is_file():
        errors.append(f"Dependency ledger missing: {docs_path}.")
    else:
        docs_text = docs_path.read_text(encoding="utf-8")
        for lib in scan["npm_cdn_libs"]:
            if lib["name"] not in docs_text or lib["version"] not in docs_text:
                errors.append(
                    f"Missing CDN library in {docs_path.name}: {lib['name']}@{lib['version']}"
                )
        for pkg in scan["py_third_party"]:
            if pkg not in docs_text:
                errors.append(f"Missing Python dependency in {docs_path.name}: {pkg}")
        for pkg in scan["ci_pip_packages"]:
            if pkg["name"] not in docs_text or pkg["version"] not in docs_text:
                errors.append(
                    f"Missing CI pip package in {docs_path.name}: {pkg['name']}=={pkg['version']}"
                )
        for act in scan["ci_actions"]:
            if (
                act["name"] not in docs_text
                or act["sha"] not in docs_text
                or act["version"] not in docs_text
            ):
                errors.append(
                    f"Missing GitHub Action in {docs_path.name}: {act['name']}@{act['sha']} ({act['version']})"
                )
        for host in scan["external_domains"]:
            if host not in docs_text:
                errors.append(f"Missing external domain in {docs_path.name}: {host}")
        for mod in scan["py_security_stdlib"]:
            if f"`{mod}`" not in docs_text:
                errors.append(
                    f"Missing security-relevant stdlib module `{mod}` in {docs_path.name}"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OBS_Widgets SBOM & dependency ledger audit")
    parser.add_argument(
        "--generate-sbom",
        action="store_true",
        help="Generate/update sbom.cdx.json from repository source",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify sbom.cdx.json and docs/DEPENDENCIES.md are up to date",
    )
    parser.add_argument(
        "--sbom-path",
        type=Path,
        default=DEFAULT_SBOM_PATH,
        help="Path to CycloneDX SBOM JSON file",
    )
    parser.add_argument(
        "--docs-path",
        type=Path,
        default=DEFAULT_DOCS_PATH,
        help="Path to dependency ledger markdown file",
    )
    args = parser.parse_args(argv)

    if not args.generate_sbom and not args.check:
        parser.print_help()
        return 2

    if args.generate_sbom:
        scan = scan_repository(REPO_ROOT)
        sbom = build_cyclonedx_sbom(scan)
        args.sbom_path.write_text(
            json.dumps(sbom, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Generated {args.sbom_path}")

    if args.check:
        errors = check_drift(REPO_ROOT, args.sbom_path, args.docs_path)
        if errors:
            for err in errors:
                print(f"[sbom_audit] ERROR: {err}", file=sys.stderr)
            return 1
        print("[sbom_audit] OK: sbom.cdx.json and docs/DEPENDENCIES.md are up to date.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
