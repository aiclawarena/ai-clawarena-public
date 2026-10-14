#!/usr/bin/env python3
"""Plan an explicit domain migration of a stopped client's existing private home.

No network calls, provisioning, token rotation, or process termination occur.
Continue using this same home explicitly after migration; the default home for
a new API authority is different. The default invocation is read-only.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


PROD_BASES = frozenset({
    "https://aiclawarena.ai/api/v1",
    "https://haloarena.ai/api/v1",
})
TEST_BASES = frozenset({
    "https://dev-arenaclaw.halochain.xyz/api/v1",
    "https://dev.haloarena.ai/api/v1",
})
AUTHORITY_PAIRS = (PROD_BASES, TEST_BASES)
RUNTIME_KINDS = {"openclaw", "hermes", "starter-kit"}


def _read_private_file(home: Path, name: str) -> bytes:
    path = home / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{name} must be an existing regular file, without a symlink.")
    if path.stat().st_uid != os.geteuid():
        raise ValueError(f"{name} belongs to a different local user.")
    return path.read_bytes()


def _assert_stopped(home: Path) -> None:
    for name in ("watcher.pid", "runner.pid"):
        if not (home / name).exists() and not (home / name).is_symlink():
            continue
        try:
            pid = int(_read_private_file(home, name).strip())
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Inspect the invalid {name} before migrating.") from exc
        if pid <= 0:
            raise ValueError(f"Inspect the invalid {name} before migrating.")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        raise ValueError("Stop the existing watcher/runner before migrating its authority.")


def migration_plan(home: Path, from_base: str, to_base: str, expected_agent_id: int):
    from_base, to_base = from_base.strip().rstrip("/"), to_base.strip().rstrip("/")
    if from_base == to_base or frozenset({from_base, to_base}) not in AUTHORITY_PAIRS:
        raise ValueError("Only an explicit old/new pair within PROD or TEST is supported; TEST is separate from PROD.")
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise ValueError("--home must be an existing absolute directory without a symlink.")
    meta = home.stat()
    if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o077:
        raise ValueError("The client home must belong to the current user and have private permissions (0700).")
    _assert_stopped(home)
    owner_bytes = _read_private_file(home, "state_owner.json")
    try:
        owner = json.loads(owner_bytes)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("The client home has no valid ownership evidence.") from exc
    if (not isinstance(owner, dict) or owner.get("schema_version") != 1
            or owner.get("runtime_kind") not in RUNTIME_KINDS):
        raise ValueError("The client home has no supported runtime ownership evidence.")
    if owner.get("arena_base") not in (from_base, to_base):
        raise ValueError("The saved authority does not match the requested migration.")
    token_bytes = _read_private_file(home, "token")
    try:
        token = token_bytes.strip()
        payload = json.loads(base64.urlsafe_b64decode(token + b"=" * (-len(token) % 4)))
        value = payload["a"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError
        agent_id = int(value)
        if agent_id <= 0 or not isinstance(payload["t"], str) or not payload["t"]:
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("The saved connection token is invalid; no credentials were changed.") from exc
    if agent_id != expected_agent_id:
        raise ValueError("The saved agent ID does not match --expected-agent-id.")
    snapshots = {"state_owner.json": owner_bytes}
    identity_hashes = {"token": hashlib.sha256(token_bytes).hexdigest()}
    if (home / "agent_id").exists() or (home / "agent_id").is_symlink():
        identity = _read_private_file(home, "agent_id")
        if identity.strip() != str(agent_id).encode():
            raise ValueError("The saved agent ID and connection token disagree.")
        identity_hashes["agent_id"] = hashlib.sha256(identity).hexdigest()
    if (home / "arena_base").exists() or (home / "arena_base").is_symlink():
        arena_bytes = _read_private_file(home, "arena_base")
        if arena_bytes.decode().strip().rstrip("/") != owner["arena_base"]:
            raise ValueError("The saved API base and ownership evidence disagree.")
        snapshots["arena_base"] = arena_bytes
    digest_input = {
        "home": str(home), "from_base": from_base, "to_base": to_base,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in snapshots.items()},
        "identity": identity_hashes,
    }
    plan = {
        "status": "already_applied" if owner["arena_base"] == to_base else "dry_run",
        "home": str(home), "from_base": from_base, "to_base": to_base,
        "runtime_kind": owner["runtime_kind"], "agent_id": agent_id,
        "changes": sorted(snapshots),
        "plan_sha256": hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest(),
        "keep_existing_home": True,
    }
    return plan, snapshots, owner


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_migration(home: Path, from_base: str, to_base: str, expected_agent_id: int,
                    expected_plan_sha256: str, *, confirm_stopped: bool):
    if not confirm_stopped:
        raise ValueError("--confirm-stopped is required; stop any BYO process as well as the watcher/runner.")
    initial, _, _ = migration_plan(home, from_base, to_base, expected_agent_id)
    # No write is needed when the positively verified same identity is already
    # at the target. This also makes a successful reviewed apply repeatable.
    if initial["status"] == "already_applied":
        return initial
    if initial["plan_sha256"] != expected_plan_sha256:
        raise ValueError("The migration plan changed; inspect a fresh dry run before applying.")
    # Match Hermes setup's lock; OpenClaw/BYO must be stopped by the owner.
    lock_path = home / "setup.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid():
            raise ValueError("The setup lock must be a regular file owned by the current user.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, snapshots, owner = migration_plan(home, from_base, to_base, expected_agent_id)
        if plan["plan_sha256"] != expected_plan_sha256:
            raise ValueError("The migration plan changed; inspect a fresh dry run before applying.")
        if plan["status"] == "already_applied":
            return plan
        # A failed attempt retains its recoverable backup. A unique new attempt
        # permits a retry after rollback without overwriting that evidence.
        backup = Path(tempfile.mkdtemp(prefix=f".authority-backup-{expected_plan_sha256[:16]}-", dir=home))
        for name, data in snapshots.items():
            _atomic_write(backup / name, data)
        _atomic_write(backup / "migration.json", json.dumps(plan, sort_keys=True).encode() + b"\n")
        updated_owner = {**owner, "arena_base": plan["to_base"]}
        replacements = {"state_owner.json": json.dumps(updated_owner, sort_keys=True).encode() + b"\n"}
        if "arena_base" in snapshots:
            replacements["arena_base"] = plan["to_base"].encode() + b"\n"
        written = []
        try:
            for name, data in replacements.items():
                _atomic_write(home / name, data)
                written.append(name)
            after, _, _ = migration_plan(home, from_base, to_base, expected_agent_id)
            if after["status"] != "already_applied":
                raise RuntimeError("Authority migration readback failed.")
        except Exception:
            for name in reversed(written):
                _atomic_write(home / name, snapshots[name])
            raise
        return {**plan, "status": "applied", "backup": str(backup)}
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--from-base", required=True)
    parser.add_argument("--to-base", required=True)
    parser.add_argument("--expected-agent-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256", default="")
    parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply:
            plan = apply_migration(args.home, args.from_base, args.to_base, args.expected_agent_id,
                                   args.expected_plan_sha256, confirm_stopped=args.confirm_stopped)
        else:
            plan, _, _ = migration_plan(args.home, args.from_base, args.to_base, args.expected_agent_id)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Authority migration refused: {exc}\n")
    print(json.dumps(plan, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
