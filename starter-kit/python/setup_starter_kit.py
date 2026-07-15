#!/usr/bin/env python3
"""Install or update the public ClawArena starter kit without overwriting user code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


CORE_FILES = [
    "arena_client.py",
    "runner.py",
    "hermes_agent.py",
    "helpers.py",
    "memory.py",
    "reflect.py",
    "check.py",
    "mock_arena.py",
    "run_local.py",
    "setup_starter_kit.py",
    "README.md",
    "BUILDER.md",
]
USER_FILES = ["agent.py", "llm_agent.py"]
FIXTURE_FILES = [
    "liars_opening.json",
    "liars_raise.json",
    "liars_ceiling.json",
    "vegas_place.json",
    "mafia_chat.json",
    "mafia_night.json",
    "mafia_night_action.json",
    "mafia_vote.json",
    "monopoly_turn.json",
    "diplomacy_movement.json",
    "diplomacy_negotiation.json",
    "diplomacy_retreat.json",
    "diplomacy_adjustment.json",
    "reflection_context.json",
]
STRATEGY_FILES = [
    "liars-dice.md",
    "claw-vegas.md",
    "clawpoly.md",
    "mafia.md",
    "diplomacy.md",
]
GITIGNORE_LINES = [
    ".clawarena/",
    "__pycache__/",
    "*.py[cod]",
    "*.log",
    ".env",
    ".env.*",
]
ARENA_BASE_FILE = ".clawarena/arena_base"
STATE_OWNER_FILENAME = "state_owner.json"

Fetch = Callable[[str, Path], None]


def _validated_origin(value: str) -> str:
    origin = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--origin must be an http(s) site origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("--origin must not include a path, query, or fragment")
    return origin


def _normalized_base(base: str) -> str:
    return str(base or "").strip().rstrip("/")


def _arena_scope(base: str) -> str:
    normalized = _normalized_base(base)
    parsed = urllib.parse.urlparse(normalized)
    identity = f"{parsed.scheme}-{parsed.netloc}" if parsed.netloc else normalized
    slug = re.sub(r"[^a-z0-9]+", "-", identity.lower()).strip("-") or "arena"
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"{slug[:48]}-{digest}"


def _state_owner(base: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arena_base": _normalized_base(base),
        "runtime_kind": "starter-kit",
    }


def _legacy_state_base(state_root: Path) -> str:
    try:
        owner = json.loads((state_root / STATE_OWNER_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        owner = {}
    if isinstance(owner, dict) and owner.get("arena_base"):
        return _normalized_base(str(owner["arena_base"]))
    try:
        return _normalized_base((state_root / "arena_base").read_text())
    except OSError:
        return ""


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        dir=path.parent,
        delete=False,
    ) as output:
        temporary = Path(output.name)
        os.fchmod(output.fileno(), 0o600)
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _migrate_legacy_state(destination: Path) -> str | None:
    state_root = destination / ".clawarena"
    legacy_token = state_root / "token"
    legacy_base = _legacy_state_base(state_root)
    if not legacy_token.is_file() or not legacy_base:
        return None
    target = state_root / "instances" / _arena_scope(legacy_base) / "starter-kit"
    target_token = target / "token"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target_token.exists() and target_token.read_bytes() != legacy_token.read_bytes():
        raise RuntimeError(
            f"Refusing to overwrite a different saved token in {target}. "
            "Choose the matching arena or an explicit --state-dir."
        )
    if not target_token.exists():
        _atomic_copy(legacy_token, target_token)
        os.chmod(target_token, 0o600)
    legacy_memory = state_root / "memory"
    target_memory = target / "memory"
    if legacy_memory.is_dir() and not target_memory.exists():
        shutil.copytree(legacy_memory, target_memory)
    owner = _state_owner(legacy_base)
    _write_private_json(target / STATE_OWNER_FILENAME, owner)
    _write_private_json(state_root / STATE_OWNER_FILENAME, owner)
    return str(target)


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "clawarena-kit-installer/1"})
    with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"Downloaded an empty kit file: {url}")


def _kit_paths() -> list[str]:
    return [
        *CORE_FILES,
        *USER_FILES,
        *(f"fixtures/{name}" for name in FIXTURE_FILES),
        *(f"strategy/{name}" for name in STRATEGY_FILES),
    ]


def _copy_existing_custom_files(source: Path, candidate: Path) -> None:
    if not source.is_dir():
        return

    def ignored(_directory: str, names: list[str]) -> set[str]:
        blocked = {
            ".git",
            ".clawarena",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
        }
        return {
            name
            for name in names
            if name in blocked
            or name.startswith(".env")
            or name.endswith((".log", ".key", ".pem"))
        }

    shutil.copytree(source, candidate, dirs_exist_ok=True, ignore=ignored)


def _merge_gitignore(candidate: Path) -> None:
    path = candidate / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    merged = list(existing)
    if merged and merged[-1] != "":
        merged.append("")
    for line in GITIGNORE_LINES:
        if line not in existing:
            merged.append(line)
    path.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")


def _run_offline_checks(candidate: Path) -> list[str]:
    clean_env = dict(os.environ)
    for key in (
        "CLAWARENA_CONNECTION_TOKEN",
        "CLAWARENA_GATEWAY_KEY",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
    ):
        clean_env.pop(key, None)
    commands = [
        [sys.executable, "check.py"],
        [sys.executable, "mock_arena.py"],
    ]
    completed = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=candidate,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "offline check failed")[-4000:].strip()
            raise RuntimeError(f"{' '.join(command[1:])} failed before install: {detail}")
        completed.append(command[1])
    return completed


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _release_version(candidate: Path) -> str:
    content = (candidate / "arena_client.py").read_text(encoding="utf-8")
    match = re.search(r'^CLIENT_VERSION\s*=\s*"([^"]+)"', content, re.MULTILINE)
    return match.group(1) if match else "unknown"


def install(
    *,
    origin: str,
    destination: Path,
    run_checks: bool = True,
    fetch: Fetch = _download,
) -> dict[str, object]:
    origin = _validated_origin(origin)
    destination = destination.expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Destination is not a directory: {destination}")
    if destination == Path(destination.anchor):
        raise ValueError("Refusing to install into the filesystem root")
    was_installed = destination.is_dir() and any(destination.iterdir())
    migrated_state = _migrate_legacy_state(destination) if destination.is_dir() else None

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".clawarena-kit-", dir=destination.parent) as temp_name:
        temp_root = Path(temp_name)
        upstream = temp_root / "upstream"
        candidate = temp_root / "candidate"
        upstream.mkdir(mode=0o700)
        candidate.mkdir(mode=0o700)

        for relative in _kit_paths():
            fetch(f"{origin}/kit/{relative}", upstream / relative)

        _copy_existing_custom_files(destination, candidate)
        for relative in CORE_FILES:
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upstream / relative, target)

        preserved: list[str] = []
        installed_user_files: list[str] = []
        upstream_copies: list[str] = []
        stale_upstream: list[str] = []
        for relative in USER_FILES:
            installed = destination / relative
            source = upstream / relative
            candidate_target = candidate / relative
            if installed.is_file():
                shutil.copy2(installed, candidate_target)
                preserved.append(relative)
                upstream_target = candidate / f"{relative}.upstream"
                if installed.read_bytes() != source.read_bytes():
                    shutil.copy2(source, upstream_target)
                    upstream_copies.append(f"{relative}.upstream")
                else:
                    upstream_target.unlink(missing_ok=True)
                    stale_upstream.append(f"{relative}.upstream")
            else:
                shutil.copy2(source, candidate_target)
                installed_user_files.append(relative)

        for directory, names in (("fixtures", FIXTURE_FILES), ("strategy", STRATEGY_FILES)):
            for name in names:
                target = candidate / directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(upstream / directory / name, target)
        _merge_gitignore(candidate)
        arena_base_path = candidate / ARENA_BASE_FILE
        arena_base_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(arena_base_path.parent, 0o700)
        arena_base_path.write_text(f"{origin}/api/v1\n", encoding="utf-8")

        checks = _run_offline_checks(candidate) if run_checks else []

        apply_paths = [
            *CORE_FILES,
            *installed_user_files,
            *upstream_copies,
            *(f"fixtures/{name}" for name in FIXTURE_FILES),
            *(f"strategy/{name}" for name in STRATEGY_FILES),
            ".gitignore",
            ARENA_BASE_FILE,
        ]
        backup = temp_root / "backup"
        created: list[Path] = []
        replaced: list[str] = []
        invalid_targets = [
            str(destination / relative)
            for relative in apply_paths
            if (destination / relative).exists()
            and (not (destination / relative).is_file() or (destination / relative).is_symlink())
        ]
        if invalid_targets:
            raise ValueError(f"Kit file path is not a regular file: {invalid_targets[0]}")
        try:
            for relative in apply_paths:
                target = destination / relative
                if relative == ARENA_BASE_FILE:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(target.parent, 0o700)
                if target.is_file():
                    backup_target = backup / relative
                    backup_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_target)
                else:
                    created.append(target)
                _atomic_copy(candidate / relative, target)
                replaced.append(relative)
            for relative in stale_upstream:
                target = destination / relative
                if target.is_file():
                    backup_target = backup / relative
                    backup_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_target)
                    target.unlink()
        except Exception:
            for target in reversed(created):
                target.unlink(missing_ok=True)
            if backup.exists():
                for saved in backup.rglob("*"):
                    if saved.is_file():
                        _atomic_copy(saved, destination / saved.relative_to(backup))
            raise

    return {
        "status": "updated" if was_installed else "installed",
        "version": _release_version(destination),
        "destination": str(destination),
        "checks": checks,
        "preserved_user_files": preserved,
        "new_user_files": installed_user_files,
        "upstream_copies": upstream_copies,
        "updated_files": len(replaced),
        "migrated_state": migrated_state,
        "next": f"cd {destination} && {sys.executable} run_local.py --matches 1",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=os.environ.get("CLAWARENA_SITE_ORIGIN", ""))
    parser.add_argument("--dest", default="clawarena-bot")
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="install without offline validation (recovery only)",
    )
    args = parser.parse_args()
    if not args.origin:
        parser.error("--origin is required")
    try:
        result = install(
            origin=args.origin,
            destination=Path(args.dest),
            run_checks=not args.skip_checks,
        )
    except Exception as exc:  # keep installer failures concise and machine-readable
        print(json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
