#!/usr/bin/env python3
"""Write or verify deterministic hashes for public runtime artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "releases" / "manifest.json"
ARTIFACTS = {
    "starter-kit/python": ROOT / "starter-kit" / "python",
    "integrations/openclaw": ROOT / "integrations" / "openclaw",
}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def release_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts)
        and path.suffix not in EXCLUDED_SUFFIXES
    )


def artifact_record(root: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    files = release_files(root)
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        total_bytes += len(data)
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        digest.update(("x" if executable else "-").encode())
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return {
        "sha256": digest.hexdigest(),
        "files": len(files),
        "bytes": total_bytes,
    }


def versions() -> dict[str, str]:
    client_source = (ARTIFACTS["starter-kit/python"] / "arena_client.py").read_text()
    client_match = re.search(r'^CLIENT_VERSION\s*=\s*"([^"]+)"', client_source, re.MULTILINE)
    if not client_match:
        raise ValueError("Starter Kit CLIENT_VERSION is missing")

    skill_dir = ARTIFACTS["integrations/openclaw"]
    skill_source = (skill_dir / "SKILL.md").read_text()
    skill_match = re.search(r"^version:\s*([^\s]+)", skill_source, re.MULTILINE)
    if not skill_match:
        raise ValueError("OpenClaw SKILL.md version is missing")
    package_version = json.loads((skill_dir / "package.json").read_text())["version"]

    found = {
        "starter_kit": client_match.group(1),
        "openclaw_skill": skill_match.group(1),
        "openclaw_package": package_version,
    }
    if len(set(found.values())) != 1:
        raise ValueError(f"release versions differ: {found}")
    return found


def current_release() -> dict[str, object]:
    return {
        "release_version": versions()["starter_kit"],
        "versions": versions(),
        "artifacts": {name: artifact_record(path) for name, path in ARTIFACTS.items()},
    }


def write_manifest(source_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("--source-commit must be a full 40-character Git SHA")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "canonical_source_commit": source_commit,
        **current_release(),
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {MANIFEST_PATH.relative_to(ROOT)} for {payload['release_version']}.")


def check_manifest() -> int:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
        current = current_release()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Release manifest check failed: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for key in ("release_version", "versions", "artifacts"):
        if manifest.get(key) != current[key]:
            errors.append(f"{key} differs from releases/manifest.json")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("canonical_source_commit", ""))):
        errors.append("canonical_source_commit is missing or not a full Git SHA")

    if errors:
        print("Release manifest check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"Release manifest verified ({current['release_version']}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--source-commit")
    args = parser.parse_args()

    if args.write:
        if not args.source_commit:
            parser.error("--write requires --source-commit")
        try:
            write_manifest(args.source_commit)
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    return check_manifest()


if __name__ == "__main__":
    raise SystemExit(main())
