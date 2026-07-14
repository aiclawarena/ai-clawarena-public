#!/usr/bin/env python3
"""Fail when private deployment material enters the public repository."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".pyc", ".pyo"}
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}

CONTENT_RULES = (
    ("private server path", re.compile(r"(?<![A-Za-z0-9_])/" + "srv/")),
    ("TEST deployment hostname", re.compile(r"\bclawarena\.halochain\.xyz\b", re.I)),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("database or Redis credential URL", re.compile(r"\b(?:postgres(?:ql)?|redis)://[^\s<>'\"]+", re.I)),
    ("GitHub token", re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "provider API key",
        re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|AIza[0-9A-Za-z_-]{30,})\b"),
    ),
)


def repository_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)
    )


def main() -> int:
    findings: list[str] = []
    for path in repository_files():
        relative = path.relative_to(ROOT)
        lower_name = path.name.lower()
        if lower_name in FORBIDDEN_NAMES or (
            lower_name.startswith(".env.") and lower_name != ".env.example"
        ):
            findings.append(f"{relative}: forbidden secret/config filename")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"{relative}: forbidden credential/generated suffix")

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in CONTENT_RULES:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: {label}")

    if findings:
        print("Public-boundary check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    print(f"Public-boundary check passed ({len(repository_files())} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
