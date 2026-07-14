"""Stable, collision-resistant local state paths for ClawArena clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse


STATE_OWNER_FILENAME = "state_owner.json"


def normalized_arena_base(api_base: str) -> str:
    return str(api_base or "").strip().rstrip("/")


def arena_scope(api_base: str) -> str:
    normalized = normalized_arena_base(api_base)
    parsed = urlparse(normalized)
    identity = f"{parsed.scheme}-{parsed.netloc}" if parsed.netloc else normalized
    slug = re.sub(r"[^a-z0-9]+", "-", identity.lower()).strip("-") or "arena"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:48]}-{digest}"


def namespaced_state_home(api_base: str, runtime_kind: str, *, root: Path) -> Path:
    return root / "instances" / arena_scope(api_base) / runtime_kind


def runtime_state_home(api_base: str, runtime_kind: str, *, root: Path) -> Path:
    """Resolve state while preserving the managed-runtime volume contract."""
    configured = str(os.environ.get("CLAWARENA_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if (
        str(os.environ.get("CLAWARENA_RUNTIME_KIND") or "").strip() == runtime_kind
        and str(os.environ.get("CLAWARENA_RUNTIME_ID") or "").strip()
    ):
        # Existing managed OpenClaw images write their mounted credential to
        # ~/.clawarena/token. Keep that isolated container contract compatible
        # while self-hosted installs use arena-scoped state by default.
        return root
    return namespaced_state_home(api_base, runtime_kind, root=root)


def state_owner(api_base: str, runtime_kind: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arena_base": normalized_arena_base(api_base),
        "runtime_kind": runtime_kind,
    }


def read_state_owner(home: Path) -> dict[str, object]:
    try:
        payload = json.loads((home / STATE_OWNER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def validate_state_owner(home: Path, api_base: str, runtime_kind: str) -> None:
    owner = read_state_owner(home)
    if not owner:
        return
    expected = state_owner(api_base, runtime_kind)
    if (
        owner.get("arena_base") != expected["arena_base"]
        or owner.get("runtime_kind") != expected["runtime_kind"]
    ):
        raise RuntimeError(
            f"ClawArena state directory {home} belongs to "
            f"{owner.get('runtime_kind') or 'another runtime'} at "
            f"{owner.get('arena_base') or 'another arena'}. Use its generated "
            "default path or choose a different explicit state directory."
        )
