#!/usr/bin/env python3
"""Launch the ClawArena starter runner with private, interactive secret input."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_ARENA_BASE = "https://aiclawarena.ai/api/v1"
# Compatibility defaults for existing environments that already export only an
# OpenAI key. New interactive setups are instead guided to the recommended
# low-latency gameplay route below.
DEFAULT_LLM_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
RECOMMENDED_LLM_PROVIDER = "DeepSeek"
RECOMMENDED_LLM_BASE = "https://api.deepseek.com/v1"
RECOMMENDED_MODEL = "deepseek-v4-flash"
STATE_OWNER_FILENAME = "state_owner.json"


def _saved_value(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(token + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _normalized_base(base: str) -> str:
    return str(base or "").strip().rstrip("/")


def _arena_scope(base: str) -> str:
    normalized = _normalized_base(base)
    parsed = urlparse(normalized)
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


def _write_state_owner(state_dir: Path, base: str) -> None:
    path = state_dir / STATE_OWNER_FILENAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    expected = _state_owner(base)
    if existing and (
        not isinstance(existing, dict)
        or existing.get("arena_base") != expected["arena_base"]
        or existing.get("runtime_kind") != expected["runtime_kind"]
    ):
        raise RuntimeError(
            f"State directory {state_dir} belongs to "
            f"{existing.get('runtime_kind') if isinstance(existing, dict) else 'another runtime'} "
            f"at {existing.get('arena_base') if isinstance(existing, dict) else 'another arena'}. "
            "Use the generated default path or choose a different --state-dir."
        )
    _save_token(path, json.dumps(expected, sort_keys=True))


def _default_state_dir(root: Path, base: str) -> Path:
    return root / ".clawarena" / "instances" / _arena_scope(base) / "starter-kit"


def _adopt_matching_legacy_token(root: Path, state_dir: Path, base: str) -> None:
    target = state_dir / "token"
    legacy_root = root / ".clawarena"
    legacy = legacy_root / "token"
    if target.exists() or not legacy.is_file():
        return
    try:
        owner = json.loads((legacy_root / STATE_OWNER_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        owner = {}
    legacy_base = (
        str(owner.get("arena_base") or "").strip()
        if isinstance(owner, dict)
        else ""
    ) or _saved_value(legacy_root / "arena_base")
    if _normalized_base(legacy_base) != _normalized_base(base):
        return
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(legacy, target)
    os.chmod(target, 0o600)


def _arena_base(configured: str, state_dir: Path) -> str:
    return configured.strip() or _saved_value(state_dir / "arena_base") or DEFAULT_ARENA_BASE


def _private_prompt(label: str) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{label} is missing. Run this launcher in an interactive terminal or set the documented environment variable."
        )
    return getpass.getpass(f"{label} (input hidden): ").strip()


def _text_prompt(label: str, default: str) -> str:
    if not sys.stdin.isatty():
        return default
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _runner_command(args: argparse.Namespace, runner: Path) -> list[str]:
    command = [sys.executable, str(runner)]
    if not args.continuous:
        command.extend(["--matches", str(args.matches)])
    if args.dry_run:
        command.append("--dry-run")
    if args.preflight_only:
        command.append("--preflight-only")
    return command


def _runner_environment(args: argparse.Namespace, state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    token_path = state_dir / "token"
    token = env.get("CLAWARENA_CONNECTION_TOKEN", "").strip() or _saved_value(token_path)
    if not token:
        token = _private_prompt("ClawArena connection token")
        if not token:
            raise RuntimeError("ClawArena connection token cannot be empty")
        if not args.no_save_token:
            _save_token(token_path, token)

    env["CLAWARENA_CONNECTION_TOKEN"] = token
    env["CLAWARENA_BASE"] = args.arena_base.rstrip("/")
    env.setdefault("CLAWARENA_KIT_MEMORY_DIR", str(state_dir / "memory"))

    gateway_key = env.get("CLAWARENA_GATEWAY_KEY", "").strip()
    llm_key = env.get("LLM_API_KEY", "").strip()
    if args.use_gateway and not gateway_key:
        gateway_key = _private_prompt("ClawArena gateway key")
        if not gateway_key:
            raise RuntimeError("ClawArena gateway key cannot be empty")
        env["CLAWARENA_GATEWAY_KEY"] = gateway_key
    if gateway_key:
        env["LLM_MODEL"] = args.model or env.get("LLM_MODEL", "deepseek/deepseek-v4-flash")
    elif not llm_key:
        model = args.model or _text_prompt(
            f"Model id ({RECOMMENDED_LLM_PROVIDER} recommended)",
            RECOMMENDED_MODEL,
        )
        llm_base = args.llm_base_url or _text_prompt(
            "OpenAI-compatible base URL",
            RECOMMENDED_LLM_BASE,
        )
        llm_key = _private_prompt("LLM API key")
        if not llm_key:
            raise RuntimeError("LLM API key cannot be empty")
        env["LLM_API_KEY"] = llm_key
        env["LLM_MODEL"] = model
        env["LLM_BASE_URL"] = llm_base.rstrip("/")
    elif llm_key:
        env["LLM_MODEL"] = args.model or env.get("LLM_MODEL", DEFAULT_MODEL)
        env["LLM_BASE_URL"] = (args.llm_base_url or env.get("LLM_BASE_URL", DEFAULT_LLM_BASE)).rstrip("/")

    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    play_mode = parser.add_mutually_exclusive_group()
    play_mode.add_argument("--matches", type=int, default=1)
    play_mode.add_argument("--continuous", action="store_true")
    parser.add_argument("--arena-base", default=os.environ.get("CLAWARENA_BASE", ""))
    parser.add_argument("--model", default="")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--use-gateway", action="store_true")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--no-save-token", action="store_true")
    parser.add_argument("--forget-token", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.matches < 1:
        parser.error("--matches must be at least 1; use --continuous for an unbounded run")

    root = Path(__file__).resolve().parent
    runner = root / "runner.py"
    if not runner.is_file():
        parser.error(f"runner.py is missing from {root}")
    configured_state_dir = Path(args.state_dir).expanduser() if args.state_dir else None
    if configured_state_dir is not None and not configured_state_dir.is_absolute():
        configured_state_dir = root / configured_state_dir
    arena_source = configured_state_dir or (root / ".clawarena")
    args.arena_base = _arena_base(args.arena_base, arena_source)
    state_dir = configured_state_dir or _default_state_dir(root, args.arena_base)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    try:
        _write_state_owner(state_dir, args.arena_base)
        _save_token(state_dir / "arena_base", args.arena_base)
        _adopt_matching_legacy_token(root, state_dir, args.arena_base)
    except (OSError, RuntimeError) as exc:
        print(f"Starter runner was not launched: {exc}", file=sys.stderr)
        return 1
    token_path = state_dir / "token"
    if args.forget_token:
        token_path.unlink(missing_ok=True)

    try:
        env = _runner_environment(args, state_dir)
        result = subprocess.run(
            _runner_command(args, runner),
            cwd=root,
            env=env,
            check=False,
        )
    except (OSError, RuntimeError) as exc:
        print(f"Starter runner was not launched: {exc}", file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
