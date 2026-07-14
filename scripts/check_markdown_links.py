#!/usr/bin/env python3
"""Check repository-local Markdown links without external dependencies."""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")


def main() -> int:
    errors: list[str] = []
    markdown_files = sorted(
        path for path in ROOT.rglob("*.md") if ".git" not in path.relative_to(ROOT).parts
    )
    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            raw_target = match.group(1).strip().strip("<>")
            if not raw_target or raw_target.startswith("#") or raw_target.startswith(EXTERNAL_SCHEMES):
                continue
            path_part = raw_target.split("#", 1)[0].split("?", 1)[0]
            path_part = urllib.parse.unquote(path_part)
            if not path_part:
                continue
            target = (ROOT / path_part.lstrip("/")) if path_part.startswith("/") else (source.parent / path_part)
            target = target.resolve()
            try:
                target.relative_to(ROOT)
            except ValueError:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{source.relative_to(ROOT)}:{line}: link escapes repository: {raw_target}")
                continue
            if not target.exists():
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{source.relative_to(ROOT)}:{line}: missing target: {raw_target}")

    if errors:
        print("Markdown link check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"Markdown link check passed ({len(markdown_files)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
