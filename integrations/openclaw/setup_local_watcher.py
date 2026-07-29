#!/usr/bin/env python3
"""Prepare and launch the ClawArena local watcher."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    from .state_paths import (
        STATE_OWNER_FILENAME,
        arena_scope,
        runtime_state_home,
        state_owner,
        validate_state_owner,
    )
except ImportError:  # Executed directly from an installed skill directory.
    from state_paths import (  # type: ignore[no-redef]
        STATE_OWNER_FILENAME,
        arena_scope,
        runtime_state_home,
        state_owner,
        validate_state_owner,
    )

API_BASE = "https://aiclawarena.ai/api/v1"
LEGACY_CLAW_DIR = Path.home() / ".clawarena"
CLAW_DIR = runtime_state_home(API_BASE, "openclaw", root=LEGACY_CLAW_DIR)
TOKEN_PATH = CLAW_DIR / "token"
AGENT_ID_PATH = CLAW_DIR / "agent_id"
CLAIM_URL_PATH = CLAW_DIR / "claim_url"
CLAIM_EXPIRES_PATH = CLAW_DIR / "claim_expires_at"
DELIVERY_CONFIG_PATH = CLAW_DIR / "openclaw_delivery.json"
OPENCLAW_AGENT_ID_PATH = CLAW_DIR / "openclaw_agent_id"
WATCHER_PID_PATH = CLAW_DIR / "watcher.pid"
WATCHER_LOG_PATH = CLAW_DIR / "watcher.log"
WATCHER_READY_PATH = CLAW_DIR / "watcher.ready"
STATE_OWNER_PATH = CLAW_DIR / STATE_OWNER_FILENAME
RECOVERY_REDEEM_URL = f"{API_BASE}/agents/connection-recovery/redeem/"
PROVISION_URL = f"{API_BASE}/agents/provision/"
CLAIM_LINK_URL = f"{API_BASE}/agents/provision/claim-link/"
SELF_HOSTED_OPENCLAW_AGENT_ID = (
    "clawarena-gameplay"
    if API_BASE.rstrip("/") == "https://aiclawarena" + ".ai/api/v1"
    else f"clawarena-gameplay-{arena_scope(API_BASE).rsplit('-', 1)[-1]}"
)


class ClawArenaAPIError(SystemExit):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"ClawArena API request failed ({status_code}): {detail}")


def stable_subprocess_cwd() -> str:
    for candidate in (Path.home(), Path("/tmp"), Path("/")):
        if candidate.exists() and candidate.is_dir():
            return str(candidate)
    return "/"


def trusted_openclaw_binary() -> str:
    """Resolve OpenClaw once so setup cannot be redirected by a later PATH change."""
    candidate = shutil.which("openclaw")
    if not candidate:
        raise RuntimeError("openclaw CLI was not found on PATH")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"openclaw CLI is not an executable file: {resolved}")
    return str(resolved)


def owned_regular_config_path(raw_path: str) -> Path:
    """Accept only the current user's regular OpenClaw config file."""
    path = Path(raw_path.strip()).expanduser()
    if not path.is_absolute():
        raise RuntimeError("OpenClaw returned a non-absolute config path")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("OpenClaw config path is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError("OpenClaw config file is not owned by the current user")
    return path


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode if mode is not None else 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watcher_process_alive(pid: int, skill_root: Path) -> bool:
    """Return true only when pid still belongs to this skill's watcher."""
    if not process_alive(pid):
        return False
    expected = str((skill_root / "watcher.py").resolve())
    proc_root = Path("/proc") / str(pid)
    if proc_root.exists():
        try:
            command = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
            return expected in command
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.returncode == 0 and expected in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def post_json(url: str, payload: dict[str, Any] | None = None, token: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except json.JSONDecodeError:
            detail = body
        raise ClawArenaAPIError(exc.code, str(detail)) from exc
    except error.URLError as exc:
        raise SystemExit(f"ClawArena API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ClawArena API returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("ClawArena API returned an unexpected response.")
    return data


def require_runtime_credentials() -> dict[str, str]:
    missing: list[str] = []
    values: dict[str, str] = {}
    for key, path in {"token": TOKEN_PATH, "agent_id": AGENT_ID_PATH}.items():
        if not path.exists():
            missing.append(str(path))
            continue
        value = path.read_text().strip()
        if not value:
            missing.append(str(path))
            continue
        values[key] = value
    if missing:
        raise SystemExit(
            "ClawArena watcher setup requires a provisioned agent first. "
            "Run setup with --provision or --recovery-key before starting the watcher. "
            f"Missing or empty: {', '.join(missing)}"
        )
    return values


def decode_connection_token_agent_id(connection_token: str) -> str:
    import base64

    padded = connection_token + ("=" * ((4 - len(connection_token) % 4) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        return str(int(payload["a"]))
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid connection token returned by recovery endpoint: {exc}") from exc


def redeem_recovery_key(recovery_key: str) -> dict[str, str]:
    payload = json.dumps({"recovery_key": recovery_key}).encode("utf-8")
    req = request.Request(
        RECOVERY_REDEEM_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Recovery key redemption failed ({exc.code}): {detail}") from exc
    except error.URLError as exc:
        raise SystemExit(f"Recovery key redemption failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Recovery endpoint returned invalid JSON: {exc}") from exc

    connection_token = str(data.get("connection_token") or "").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    if not connection_token:
        raise SystemExit("Recovery endpoint did not return a connection_token.")
    decoded_agent_id = decode_connection_token_agent_id(connection_token)
    if agent_id and agent_id != decoded_agent_id:
        raise SystemExit("Recovery endpoint returned mismatched agent IDs.")
    return {
        "token": connection_token,
        "agent_id": agent_id or decoded_agent_id,
        "agent_name": str(data.get("agent_name") or ""),
    }


def write_runtime_credentials(credentials: dict[str, str]) -> None:
    atomic_write(TOKEN_PATH, credentials["token"].strip() + "\n", 0o600)
    atomic_write(AGENT_ID_PATH, credentials["agent_id"].strip() + "\n", 0o600)


def write_state_owner() -> None:
    validate_state_owner(CLAW_DIR, API_BASE, "openclaw")
    atomic_write(
        STATE_OWNER_PATH,
        json.dumps(state_owner(API_BASE, "openclaw"), sort_keys=True) + "\n",
        0o600,
    )


def legacy_openclaw_home() -> Path | None:
    if CLAW_DIR.resolve() == LEGACY_CLAW_DIR.resolve() or TOKEN_PATH.exists():
        return None
    if not (LEGACY_CLAW_DIR / "token").is_file():
        return None
    signatures = (
        "openclaw_agent_id",
        "openclaw_delivery.json",
        "watcher.pid",
        "watcher.log",
        "watcher_state.json",
        "openclaw-workspace",
    )
    if not any((LEGACY_CLAW_DIR / name).exists() for name in signatures):
        return None
    return LEGACY_CLAW_DIR


def copy_legacy_openclaw_state(source: Path) -> None:
    CLAW_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_names = (
        "token",
        "agent_id",
        "claim_url",
        "claim_expires_at",
        "openclaw_delivery.json",
        "openclaw_agent_id",
        "watcher.pid",
        "watcher.log",
        "watcher_state.json",
    )
    for name in file_names:
        source_path = source / name
        target_path = CLAW_DIR / name
        if source_path.is_file() and not target_path.exists():
            shutil.copy2(source_path, target_path)
    source_workspace = source / "openclaw-workspace"
    target_workspace = CLAW_DIR / "openclaw-workspace"
    if source_workspace.is_dir() and not target_workspace.exists():
        shutil.copytree(source_workspace, target_workspace)


def save_claim_state(payload: dict[str, Any]) -> dict[str, Any]:
    claim_url = str(payload.get("claim_url") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip()
    if claim_url:
        atomic_write(CLAIM_URL_PATH, claim_url + "\n", 0o600)
    else:
        CLAIM_URL_PATH.unlink(missing_ok=True)
    if expires_at:
        atomic_write(CLAIM_EXPIRES_PATH, expires_at + "\n", 0o600)
    else:
        CLAIM_EXPIRES_PATH.unlink(missing_ok=True)
    return {
        "claim_url": claim_url or None,
        "claim_expires_at": expires_at or None,
        "agent_claimed": bool(payload.get("agent_claimed")),
        "claim_refreshed": bool(payload.get("refreshed")),
    }


def provision_or_refresh() -> tuple[dict[str, str], dict[str, Any]]:
    token = TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else ""
    reused = bool(token)
    migrated_from: str | None = None
    legacy_payload: dict[str, Any] | None = None
    if not token:
        legacy_home = legacy_openclaw_home()
        if legacy_home is not None:
            legacy_token = (legacy_home / "token").read_text().strip()
            try:
                legacy_payload = post_json(CLAIM_LINK_URL, token=legacy_token)
            except ClawArenaAPIError as exc:
                if exc.status_code not in {401, 403, 404}:
                    raise
            else:
                copy_legacy_openclaw_state(legacy_home)
                token = legacy_token
                reused = True
                migrated_from = str(legacy_home)
    if token:
        payload = legacy_payload or post_json(CLAIM_LINK_URL, token=token)
        agent_id = str(payload.get("agent_id") or decode_connection_token_agent_id(token))
        credentials = {"token": token, "agent_id": agent_id}
    else:
        name = f"openclaw-{os.uname().nodename[:12]}-{secrets.token_hex(2)}"
        payload = post_json(PROVISION_URL, {
            "name": name,
            "runtime_kind": "openclaw",
        })
        connection_token = str(payload.get("connection_token") or "").strip()
        if not connection_token:
            raise SystemExit("Provisioning response did not include a connection_token.")
        credentials = {
            "token": connection_token,
            "agent_id": str(payload.get("agent_id") or decode_connection_token_agent_id(connection_token)),
        }
    write_runtime_credentials(credentials)
    claim_state = save_claim_state(payload)
    claim_state.update(
        agent_reused=reused,
        agent_provisioned=not reused,
        state_migrated_from=migrated_from,
    )
    return credentials, claim_state


def refresh_claim_state(credentials: dict[str, str]) -> dict[str, Any]:
    payload = post_json(CLAIM_LINK_URL, token=credentials["token"])
    response_agent_id = str(payload.get("agent_id") or "").strip()
    if response_agent_id and response_agent_id != credentials["agent_id"]:
        raise SystemExit("Claim-link endpoint returned a mismatched agent id.")
    return save_claim_state(payload)


def stop_existing_watcher(skill_root: Path) -> None:
    if not WATCHER_PID_PATH.exists():
        return
    try:
        pid = int(WATCHER_PID_PATH.read_text().strip())
    except ValueError:
        WATCHER_PID_PATH.unlink(missing_ok=True)
        return
    if watcher_process_alive(pid, skill_root):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not process_alive(pid):
                break
            time.sleep(0.2)
    WATCHER_PID_PATH.unlink(missing_ok=True)


def write_delivery_config(args: argparse.Namespace) -> dict[str, Any]:
    existing = read_json(DELIVERY_CONFIG_PATH)
    channel = args.channel or existing.get("channel")
    target = args.to or existing.get("to")
    reply_account = args.reply_account or existing.get("reply_account")
    if not channel or not target:
        raise SystemExit(
            "channel and to are required on first setup; reruns can reuse the saved config."
        )
    config = {
        "channel": channel,
        "to": target,
    }
    if reply_account:
        config["reply_account"] = reply_account
    atomic_write(
        DELIVERY_CONFIG_PATH,
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        0o600,
    )
    return config


def configure_restricted_openclaw_agent(skill_root: Path) -> dict[str, Any]:
    """One-click setup for a restricted self-hosted gameplay agent."""
    explicit_agent_id = str(os.environ.get("CLAWARENA_OPENCLAW_AGENT_ID", "")).strip()
    if explicit_agent_id:
        return {
            "agent_id": explicit_agent_id,
            "configured": True,
            "automatic": False,
            "warning": None,
        }

    arena_api_path = (skill_root / "arena_api.py").resolve()
    workspace = (CLAW_DIR / "openclaw-workspace").resolve()
    agent_id = SELF_HOSTED_OPENCLAW_AGENT_ID
    created = False
    openclaw_bin = ""

    def run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=stable_subprocess_cwd(),
        )

    try:
        openclaw_bin = trusted_openclaw_binary()
        arena_api_path.chmod(0o700)
        listed = run([openclaw_bin, "agents", "list", "--json"])
        if listed.returncode != 0:
            raise RuntimeError((listed.stderr or listed.stdout or "agents list failed").strip())
        agents = json.loads(listed.stdout or "[]")
        if not isinstance(agents, list):
            raise RuntimeError("OpenClaw agents list returned an unexpected payload")
        if not any(str(entry.get("id") or "") == agent_id for entry in agents if isinstance(entry, dict)):
            added = run([
                openclaw_bin, "agents", "add", agent_id,
                "--workspace", str(workspace),
                "--non-interactive", "--json",
            ])
            if added.returncode != 0:
                raise RuntimeError((added.stderr or added.stdout or "agents add failed").strip())
            created = True

        config_file = run([openclaw_bin, "config", "file"])
        if config_file.returncode != 0:
            raise RuntimeError((config_file.stderr or config_file.stdout or "config file failed").strip())
        config_path = owned_regular_config_path(config_file.stdout or "")
        config = json.loads(config_path.read_text())
        agents_config = config.get("agents") or {}
        mapped_entries = agents_config.get("entries")
        listed_entries = agents_config.get("list")
        if isinstance(mapped_entries, dict):
            persisted = mapped_entries.get(agent_id)
            if not isinstance(persisted, dict):
                raise RuntimeError("OpenClaw created the agent but did not persist its config entry")
            entry = dict(persisted)
            config_key = f"agents.entries.{agent_id}"
        elif isinstance(listed_entries, list):
            index = next(
                (
                    i
                    for i, candidate in enumerate(listed_entries)
                    if isinstance(candidate, dict)
                    and str(candidate.get("id") or "") == agent_id
                ),
                None,
            )
            if index is None:
                raise RuntimeError("OpenClaw created the agent but did not persist its config entry")
            entry = dict(listed_entries[index])
            config_key = f"agents.list[{index}]"
        else:
            raise RuntimeError("OpenClaw returned an unsupported agents config schema")
        configured_workspace = Path(str(entry.get("workspace") or "")).expanduser().resolve()
        if not created and configured_workspace != workspace:
            legacy_workspace = (LEGACY_CLAW_DIR / "openclaw-workspace").resolve()
            if configured_workspace != legacy_workspace or not TOKEN_PATH.exists():
                raise RuntimeError(
                    f'OpenClaw agent id "{agent_id}" already belongs to another workspace'
                )
            entry["workspace"] = str(workspace)
        entry.update({
            "name": "ClawArena Gameplay",
            "skills": [],
            "tools": {
                "allow": ["exec", "process"],
                "exec": {
                    "host": "gateway",
                    "security": "allowlist",
                    "ask": "off",
                    "strictInlineEval": True,
                },
            },
        })
        updated = run([
            openclaw_bin, "config", "set", config_key,
            json.dumps(entry, separators=(",", ":")), "--strict-json",
        ])
        if updated.returncode != 0:
            raise RuntimeError((updated.stderr or updated.stdout or "config update failed").strip())
        approved = run([
            openclaw_bin, "approvals", "allowlist", "add",
            "--agent", agent_id, str(arena_api_path),
        ])
        if approved.returncode != 0:
            raise RuntimeError((approved.stderr or approved.stdout or "allowlist update failed").strip())
        validated = run([openclaw_bin, "config", "validate"])
        if validated.returncode != 0:
            raise RuntimeError((validated.stderr or validated.stdout or "config validation failed").strip())
        workspace.mkdir(parents=True, exist_ok=True)
        for bootstrap_name in (
            "AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "HEARTBEAT.md", "TOOLS.md",
        ):
            atomic_write(workspace / bootstrap_name, "", 0o600)
        return {
            "agent_id": agent_id,
            "configured": True,
            "automatic": True,
            "created": created,
            "warning": None,
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as exc:
        return {
            "agent_id": "",
            "configured": False,
            "automatic": True,
            "created": created,
            "warning": f"Restricted OpenClaw agent unavailable: {exc}",
        }


def verify_delivery(
    config: dict[str, Any],
    *,
    openclaw_agent_id: str | None = None,
) -> dict[str, Any]:
    cmd = [
        trusted_openclaw_binary(),
        "agent",
    ]
    if openclaw_agent_id is None:
        openclaw_agent_id = str(os.environ.get("CLAWARENA_OPENCLAW_AGENT_ID", "")).strip()
    if openclaw_agent_id:
        cmd.extend(["--agent", openclaw_agent_id])
    cmd.extend([
        "--local",
        "--session-id",
        f"clawarena-setup-{int(time.time())}",
        "--message",
        "ClawArena delivery test. Reply with exactly: ClawArena delivery OK.",
        "--deliver",
        "--reply-channel",
        str(config["channel"]),
        "--reply-to",
        str(config["to"]),
        "--json",
    ])
    reply_account = config.get("reply_account")
    if reply_account:
        cmd.extend(["--reply-account", str(reply_account)])

    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=stable_subprocess_cwd(),
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Delivery verification failed: {exc}") from exc

    output = (proc.stdout or proc.stderr or "").strip()
    result = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output[:1000],
    }
    if proc.returncode != 0:
        raise SystemExit(
            "Delivery verification failed. OpenClaw could not deliver back "
            f"to this chat. Output: {output[:1000]}"
        )
    return result


def _log_tail(path: Path, limit: int = 1200) -> str:
    try:
        return path.read_text(errors="replace")[-limit:].strip()
    except OSError:
        return ""


def preflight_watcher(
    skill_root: Path,
    credentials: dict[str, str],
    *,
    openclaw_agent_id: str = "",
) -> None:
    """Probe the candidate watcher without touching the live watcher's files."""
    CLAW_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".watcher-preflight-", dir=CLAW_DIR) as temp_name:
        temp_home = Path(temp_name)
        temp_home.chmod(0o700)
        atomic_write(temp_home / "token", credentials["token"].strip() + "\n", 0o600)
        ready_path = temp_home / "watcher.ready"
        env = dict(os.environ)
        env.update(
            CLAWARENA_HOME=str(temp_home),
            CLAWARENA_READY_FILE=str(ready_path),
            CLAWARENA_OPENCLAW_BIN=trusted_openclaw_binary(),
        )
        if openclaw_agent_id:
            env["CLAWARENA_OPENCLAW_AGENT_ID"] = openclaw_agent_id
        else:
            env.pop("CLAWARENA_OPENCLAW_AGENT_ID", None)
        try:
            result = subprocess.run(  # noqa: S603
                [sys.executable, str(skill_root / "watcher.py"), "--preflight"],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
                cwd=str(skill_root),
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemExit(f"Candidate watcher preflight failed: {exc}") from exc
        if result.returncode != 0 or not ready_path.exists():
            detail = (result.stderr or result.stdout or "server readiness was not confirmed")[-1200:].strip()
            raise SystemExit(f"Candidate watcher preflight failed: {detail}")


def start_watcher(skill_root: Path, *, openclaw_agent_id: str = "") -> subprocess.Popen:
    WATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    watcher_path = skill_root / "watcher.py"
    WATCHER_READY_PATH.unlink(missing_ok=True)
    env = dict(os.environ)
    env.update(
        CLAWARENA_HOME=str(CLAW_DIR),
        CLAWARENA_READY_FILE=str(WATCHER_READY_PATH),
        CLAWARENA_OPENCLAW_BIN=trusted_openclaw_binary(),
    )
    if openclaw_agent_id:
        env["CLAWARENA_OPENCLAW_AGENT_ID"] = openclaw_agent_id
    else:
        env.pop("CLAWARENA_OPENCLAW_AGENT_ID", None)
    log_fd = os.open(WATCHER_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(WATCHER_LOG_PATH, 0o600)
    with os.fdopen(log_fd, "ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(watcher_path)],
            stdout=log_file,
            stderr=log_file,
            cwd=str(skill_root),
            env=env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    atomic_write(WATCHER_PID_PATH, f"{proc.pid}\n", 0o600)
    return proc


def wait_for_watcher_ready(proc: subprocess.Popen, timeout: float = 35.0) -> None:
    deadline = time.monotonic() + timeout
    while proc.poll() is None and not WATCHER_READY_PATH.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None and WATCHER_READY_PATH.exists():
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        pass
    WATCHER_PID_PATH.unlink(missing_ok=True)
    if proc.poll() is not None:
        message = f"watcher exited during startup (code {proc.returncode})"
    else:
        message = f"watcher did not report server readiness within {int(timeout)}s"
    tail = _log_tail(WATCHER_LOG_PATH)
    raise SystemExit(f"{message}. Log: {tail or WATCHER_LOG_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up the ClawArena local watcher")
    parser.add_argument("--channel", help="Active OpenClaw channel for delivery, e.g. telegram")
    parser.add_argument("--to", help="Active chat target, e.g. a Telegram numeric chat id")
    parser.add_argument(
        "--provision",
        action="store_true",
        help="Provision once, or reuse the saved token and refresh its pending claim link.",
    )
    parser.add_argument(
        "--accept-persistent-setup",
        action="store_true",
        help=(
            "Confirm first-time setup may store a scoped token, create a restricted "
            "OpenClaw agent approval, and start a background watcher."
        ),
    )
    parser.add_argument(
        "--recovery-key",
        help="One-use recovery key from Command Center. Redeems it, saves fresh credentials, and restarts the watcher.",
    )
    parser.add_argument(
        "--connection-token",
        help="Fresh connection token to save before starting the watcher. Prefer --recovery-key for user recovery.",
    )
    parser.add_argument("--agent-id", help="Agent id for --connection-token. If omitted, it is decoded from the token.")
    parser.add_argument(
        "--reply-account",
        help="Optional OpenClaw account id for outbound delivery",
    )
    parser.add_argument(
        "--verify-delivery",
        action="store_true",
        help="Send a short OpenClaw delivery test to the configured chat before starting the watcher.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Managed-runtime mode. Save credentials and start the watcher without delivery config.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop this arena's local watcher without changing credentials.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_root = Path(__file__).resolve().parent
    validate_state_owner(CLAW_DIR, API_BASE, "openclaw")
    if args.stop:
        stop_existing_watcher(skill_root)
        print(json.dumps({"watcher_stopped": True, "home": str(CLAW_DIR)}))
        return 0
    if args.provision and not TOKEN_PATH.exists() and not args.accept_persistent_setup:
        raise SystemExit(
            "First-time ClawArena setup requires --accept-persistent-setup after the "
            "user reviews its scoped credential storage, restricted exec approval, "
            "and background watcher."
        )
    recovery_applied = False
    claim_state: dict[str, Any] = {
        "claim_url": None,
        "claim_expires_at": None,
        "agent_claimed": False,
        "claim_refreshed": False,
        "agent_reused": True,
        "agent_provisioned": False,
    }
    if args.recovery_key:
        credentials = redeem_recovery_key(args.recovery_key)
        write_runtime_credentials(credentials)
        recovery_applied = True
    elif args.connection_token:
        credentials = {
            "token": args.connection_token,
            "agent_id": args.agent_id or decode_connection_token_agent_id(args.connection_token),
        }
        write_runtime_credentials(credentials)
        recovery_applied = True
    elif args.provision:
        credentials, claim_state = provision_or_refresh()
    else:
        credentials = require_runtime_credentials()
    if recovery_applied:
        claim_state = refresh_claim_state(credentials)
        claim_state.update(agent_reused=True, agent_provisioned=False)
    write_state_owner()
    explicit_openclaw_agent_id = str(os.environ.get("CLAWARENA_OPENCLAW_AGENT_ID", "")).strip()
    saved_openclaw_agent_id = ""
    if not explicit_openclaw_agent_id:
        try:
            saved_openclaw_agent_id = OPENCLAW_AGENT_ID_PATH.read_text().strip()
        except OSError:
            pass
    restricted_agent = {
        "agent_id": explicit_openclaw_agent_id or saved_openclaw_agent_id,
        "configured": bool(explicit_openclaw_agent_id or saved_openclaw_agent_id),
        "automatic": bool(saved_openclaw_agent_id and not explicit_openclaw_agent_id),
        "warning": None,
    }
    selected_openclaw_agent_id = str(restricted_agent["agent_id"] or "")
    if not explicit_openclaw_agent_id:
        restricted_agent = configure_restricted_openclaw_agent(skill_root)
        selected_openclaw_agent_id = str(restricted_agent.get("agent_id") or "")
    if not selected_openclaw_agent_id or not restricted_agent.get("configured"):
        detail = str(restricted_agent.get("warning") or "unknown setup error")
        if not saved_openclaw_agent_id:
            # Versions before 5.12.1 could start the watcher on the default
            # OpenClaw agent and then remove this marker. Do not leave that
            # broad-tool legacy process running when isolation cannot be
            # established. A watcher with a saved restricted id is safe to
            # preserve through a transient setup/auth failure.
            stop_existing_watcher(skill_root)
        raise SystemExit(
            "ClawArena requires a restricted OpenClaw gameplay agent and will not "
            f"fall back to the default agent. {detail}"
        )
    if args.headless:
        config = {}
        delivery_verification = None
    else:
        config = write_delivery_config(args)
        delivery_verification = None
        if args.verify_delivery:
            try:
                delivery_verification = verify_delivery(
                    config,
                    openclaw_agent_id=selected_openclaw_agent_id,
                )
            except SystemExit:
                if not saved_openclaw_agent_id:
                    stop_existing_watcher(skill_root)
                raise
    preflight_watcher(
        skill_root,
        credentials,
        openclaw_agent_id=selected_openclaw_agent_id,
    )
    atomic_write(OPENCLAW_AGENT_ID_PATH, selected_openclaw_agent_id + "\n", 0o600)
    stop_existing_watcher(skill_root)
    proc = start_watcher(skill_root, openclaw_agent_id=selected_openclaw_agent_id)
    wait_for_watcher_ready(proc)
    print(
        json.dumps(
            {
                "watcher_started": True,
                "recovery_applied": recovery_applied,
                "pid": proc.pid,
                "agent_id": credentials["agent_id"],
                **claim_state,
                "headless": args.headless,
                "channel": config.get("channel"),
                "to": config.get("to"),
                "reply_account": config.get("reply_account"),
                "delivery_verification": delivery_verification,
                "openclaw_agent_id": selected_openclaw_agent_id or None,
                "restricted_agent_enabled": bool(selected_openclaw_agent_id),
                "restricted_agent_warning": restricted_agent.get("warning"),
                "watcher_script": str(skill_root / "watcher.py"),
                "log_file": str(WATCHER_LOG_PATH),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
