#!/usr/bin/env python3
"""Lightweight, dependency-free validation for the published API contract."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "openapi" / "agent-api-v1.json"
REQUIRED_PATHS = {
    "/agents/schema/",
    "/agents/provision/",
    "/agents/game/",
    "/agents/action/",
    "/agents/watcher/",
    "/agents/strategy-reflection/",
    "/agents/strategy-prompt/",
}


def walk(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk(nested)


def client_version() -> str:
    source = (ROOT / "starter-kit" / "python" / "arena_client.py").read_text()
    match = re.search(r'^CLIENT_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        raise ValueError("CLIENT_VERSION is missing")
    return match.group(1)


def main() -> int:
    errors: list[str] = []
    try:
        spec = json.loads(SPEC_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"OpenAPI parse failed: {exc}", file=sys.stderr)
        return 1

    if spec.get("openapi") != "3.1.0":
        errors.append("openapi must be 3.1.0")
    if spec.get("info", {}).get("version") != client_version():
        errors.append("OpenAPI info.version must match Starter Kit CLIENT_VERSION")
    missing_paths = REQUIRED_PATHS - set(spec.get("paths", {}))
    if missing_paths:
        errors.append(f"missing paths: {', '.join(sorted(missing_paths))}")

    operation_ids: list[str] = []
    for path_item in spec.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                operation_id = operation.get("operationId")
                if not operation_id:
                    errors.append(f"operation without operationId: {method}")
                else:
                    operation_ids.append(operation_id)
    if len(operation_ids) != len(set(operation_ids)):
        errors.append("operationId values must be unique")

    for node in walk(spec):
        reference = node.get("$ref")
        if not isinstance(reference, str) or reference.startswith("#"):
            continue
        file_part = reference.split("#", 1)[0]
        target = (SPEC_PATH.parent / file_part).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            errors.append(f"external reference escapes repository: {reference}")
            continue
        if not target.is_file():
            errors.append(f"missing referenced schema: {reference}")
            continue
        try:
            json.loads(target.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid referenced JSON {reference}: {exc}")

    for schema_path in sorted((ROOT / "schemas").glob("*.json")):
        try:
            schema = json.loads(schema_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON Schema {schema_path.name}: {exc}")
            continue
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{schema_path.name}: unexpected JSON Schema dialect")

    if errors:
        print("OpenAPI check failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"OpenAPI check passed ({len(operation_ids)} operations).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
