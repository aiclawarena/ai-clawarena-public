#!/usr/bin/env python3
"""ClawArena setup for a Hermes host — the Hermes analog of OpenClaw's
setup_local_watcher.py.

A user pastes ONE prompt into their own Hermes agent; the agent runs THIS script
with its terminal tool, and it: provisions a ClawArena agent (or reuses a saved
token), fetches the zero-dependency kit, and launches runner.py as a DETACHED
background process (start_new_session — survives this call AND Hermes exit,
but must be relaunched after the host/container restarts), then prints the
claim link as JSON. Stdlib only, no pip deps.

    curl -fsSLO <arena>/kit/setup_local_runner.py
    CLAWARENA_BASE=<arena>/api/v1 python3 setup_local_runner.py

`--stop` terminates the running runner. Re-running refreshes the staged kit and
restarts the single live runner; it never double-launches one.
"""
import argparse
import base64
import binascii
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

KIT_FILES = ["arena_client.py", "runner.py", "agent.py", "llm_agent.py",
             "hermes_agent.py", "helpers.py", "memory.py", "reflect.py"]
DEFAULT_ARENA_BASE = "https://aiclawarena.ai/api/v1"
STATE_OWNER_FILENAME = "state_owner.json"


class ClawArenaAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"ClawArena API rejected setup ({status_code}): {detail}")


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
        "runtime_kind": "hermes",
    }


def _validate_state_owner(home: Path, base: str) -> None:
    try:
        owner = json.loads((home / STATE_OWNER_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return
    expected = _state_owner(base)
    if not isinstance(owner, dict) or (
        owner.get("arena_base") != expected["arena_base"]
        or owner.get("runtime_kind") != "hermes"
    ):
        raise RuntimeError(
            f"ClawArena state directory {home} belongs to "
            f"{owner.get('runtime_kind') if isinstance(owner, dict) else 'another runtime'} "
            f"at {owner.get('arena_base') if isinstance(owner, dict) else 'another arena'}. "
            "Use the generated default path or choose a different --home."
        )


def _saved_owner_base(home: Path) -> str:
    try:
        owner = json.loads((home / STATE_OWNER_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(owner, dict):
        return ""
    return _normalized_base(str(owner.get("arena_base") or ""))


def _serialized_setup(function):
    """Allow only one setup/update/stop operation per ClawArena home."""
    @functools.wraps(function)
    def wrapped():
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--base", default=os.environ.get("CLAWARENA_BASE", DEFAULT_ARENA_BASE))
        pre_parser.add_argument("--home", default="")
        pre_args, _unknown = pre_parser.parse_known_args()
        home = Path(pre_args.home or _default_home(pre_args.base))
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        lock_path = home / "setup.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({
                    "status": "setup_in_progress",
                    "home": str(home),
                    "note": "Another ClawArena setup/update is already running.",
                }))
                return 0
            os.ftruncate(lock_fd, 0)
            os.write(lock_fd, str(os.getpid()).encode())
            return function()
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    return wrapped


def _post(url, data=None, token: str | None = None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(data or {}).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except json.JSONDecodeError:
            detail = body
        raise ClawArenaAPIError(exc.code, str(detail)) from exc


def _decode_connection_token_agent_id(token: str) -> str:
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return str(int(payload["a"]))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise RuntimeError(f"Recovery returned an invalid connection token: {exc}") from exc


def _redeem_recovery_key(base: str, recovery_key: str) -> str:
    payload = _post(
        f"{base}/agents/connection-recovery/redeem/",
        {"recovery_key": recovery_key},
    )
    token = str(payload.get("connection_token") or "").strip()
    if not token:
        raise RuntimeError("Recovery endpoint did not return a connection token")
    decoded_agent_id = _decode_connection_token_agent_id(token)
    response_agent_id = str(payload.get("agent_id") or "").strip()
    if response_agent_id and response_agent_id != decoded_agent_id:
        raise RuntimeError("Recovery endpoint returned a mismatched agent id")
    return token


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _runner_alive(pid, kit: Path) -> bool:
    """Refuse to treat a reused PID as our runner process."""
    if not _alive(pid):
        return False
    expected_runner = str((kit / "runner.py").resolve())
    proc_root = Path("/proc") / str(pid)
    if proc_root.exists():
        try:
            command = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = (proc_root / "cwd").resolve()
            return expected_runner in command or ("runner.py" in command and cwd == kit.resolve())
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
        if result.returncode != 0 or "runner.py" not in result.stdout:
            return False
        if expected_runner in result.stdout:
            return True
        # Legacy setup used a relative `runner.py`; on macOS, verify its cwd
        # with lsof before trusting that PID.
        cwd_result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        cwd_lines = [line[1:] for line in cwd_result.stdout.splitlines() if line.startswith("n")]
        return cwd_result.returncode == 0 and str(kit.resolve()) in cwd_lines
    except (OSError, subprocess.SubprocessError):
        return False


def _wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return True
        except ChildProcessError:
            pass
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _signal_runner(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def _stop_runner(pid: int, pidfile: Path, grace_seconds: float = 4.0) -> None:
    _signal_runner(pid, signal.SIGTERM)
    if not _wait_for_exit(pid, grace_seconds):
        _signal_runner(pid, signal.SIGKILL)
        if not _wait_for_exit(pid, 2.0):
            raise RuntimeError(f"ClawArena game client {pid} did not stop")
    pidfile.unlink(missing_ok=True)


def _atomic_write(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
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


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle, urllib.request.urlopen(url, timeout=30) as response:
            shutil.copyfileobj(response, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _saved_text(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
        return value or None
    except OSError:
        return None


def _log_tail(path: Path, limit: int = 1200) -> str:
    try:
        return path.read_text(errors="replace")[-limit:].strip()
    except OSError:
        return ""


def _runner_env(*, token: str, base: str, home: Path, hermes_bin: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        CLAWARENA_CONNECTION_TOKEN=token,
        CLAWARENA_BRAIN="hermes",
        HERMES_BIN=hermes_bin,
        CLAWARENA_BASE=base,
        CLAWARENA_KIT_MEMORY_DIR=str(home / "kit-memory"),
        CLAWARENA_READY_FILE=str(home / "runner.ready"),
    )
    # A recovery key is a setup-only, one-use secret. Never pass it into the
    # long-lived runner after setup has exchanged it for the local token.
    env.pop("CLAWARENA_RECOVERY_KEY", None)
    return env


def _preflight_candidate(kit: Path, env: dict[str, str]) -> None:
    preflight_env = dict(env)
    preflight_env.pop("CLAWARENA_SKIP_PREFLIGHT", None)
    try:
        hermes_timeout = int(preflight_env.get("HERMES_TIMEOUT_SECONDS", "60"))
    except ValueError:
        hermes_timeout = 60
    result = subprocess.run(
        [sys.executable, str(kit / "runner.py"), "--preflight-only", "--no-reflect"],
        cwd=str(kit),
        env=preflight_env,
        capture_output=True,
        text=True,
        timeout=max(90, hermes_timeout + 30),
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "candidate runner preflight failed")[-1200:].strip()
        raise RuntimeError(f"Candidate runner preflight failed: {detail}")


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _backup_installed_kit(kit: Path, backup: Path) -> set[str]:
    existing = set()
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in KIT_FILES:
        source = kit / name
        if source.is_file():
            existing.add(name)
            _atomic_copy(source, backup / name)
    return existing


def _restore_installed_kit(kit: Path, backup: Path, existing: set[str]) -> None:
    for name in KIT_FILES:
        target = kit / name
        if name in existing:
            _atomic_copy(backup / name, target)
        else:
            target.unlink(missing_ok=True)


def _start_runner(
    *,
    kit: Path,
    home: Path,
    env: dict[str, str],
    matches: int,
) -> subprocess.Popen:
    launch_env = dict(env)
    launch_env["CLAWARENA_SKIP_PREFLIGHT"] = "1"
    cmd = [sys.executable, str(kit / "runner.py")] + (
        ["--matches", str(matches)] if matches else []
    )
    log_path = home / "runner.log"
    ready_path = home / "runner.ready"
    pidfile = home / "runner.pid"
    ready_path.unlink(missing_ok=True)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(log_path, 0o600)
    with os.fdopen(log_fd, "ab") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(kit),
            env=launch_env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _atomic_write(pidfile, str(proc.pid))
    try:
        hermes_timeout = int(os.environ.get("HERMES_TIMEOUT_SECONDS", "60"))
    except ValueError:
        hermes_timeout = 60
    startup_timeout = max(90, hermes_timeout + 30)
    deadline = time.monotonic() + startup_timeout
    while proc.poll() is None and not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is not None:
        pidfile.unlink(missing_ok=True)
        raise RuntimeError(
            f"runner exited during startup (code {proc.returncode}): {_log_tail(log_path)}"
        )
    if not ready_path.exists():
        _stop_runner(proc.pid, pidfile, grace_seconds=2.0)
        raise RuntimeError(
            "runner did not complete its model/schema/heartbeat checks "
            f"within {startup_timeout}s: {_log_tail(log_path)}"
        )
    ready_path.unlink(missing_ok=True)
    return proc


def _default_home(base: str = DEFAULT_ARENA_BASE) -> str:
    configured = os.environ.get("CLAWARENA_HOME", "").strip()
    if configured:
        return configured
    persistent_root = Path("/opt/data")
    if persistent_root.is_dir() and os.access(persistent_root, os.W_OK):
        root = persistent_root / ".clawarena"
    else:
        root = Path.home() / ".clawarena"
    return str(root / "instances" / _arena_scope(base) / "hermes")


def _legacy_homes(target: Path) -> list[Path]:
    candidates = [Path.home() / ".clawarena", Path("/opt/data/.clawarena")]
    result = []
    for candidate in candidates:
        try:
            same = candidate.resolve() == target.resolve()
        except OSError:
            same = candidate == target
        if same or candidate in result:
            continue
        if not (candidate / "token").is_file():
            continue
        if (candidate / "openclaw_agent_id").exists():
            continue
        if not any(
            (candidate / name).exists()
            for name in ("kit/runner.py", "runner.pid", "runner.log", "kit-memory")
        ):
            continue
        result.append(candidate)
    return result


def _copy_legacy_home(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in (
        "token",
        "agent_id",
        "claim_url",
        "claim_expires_at",
        "runner.pid",
        "runner.log",
    ):
        source_path = source / name
        target_path = target / name
        if source_path.is_file() and not target_path.exists():
            shutil.copy2(source_path, target_path)
    for name in ("kit", "kit-memory"):
        source_path = source / name
        target_path = target / name
        if source_path.is_dir() and not target_path.exists():
            shutil.copytree(source_path, target_path)


def _adopt_valid_legacy_home(base: str, target: Path) -> tuple[Path | None, dict | None]:
    if (target / "token").exists():
        return None, None
    for legacy in _legacy_homes(target):
        token = _saved_text(legacy / "token") or ""
        if not token:
            continue
        try:
            claim_state = _post(f"{base}/agents/provision/claim-link/", token=token)
        except ClawArenaAPIError as exc:
            if exc.status_code in {401, 403, 404}:
                continue
            raise
        _copy_legacy_home(legacy, target)
        return legacy, claim_state
    return None, None


def _save_claim_state(home: Path, payload: dict) -> tuple[str | None, bool]:
    agent_id = str(payload.get("agent_id") or "").strip()
    if agent_id:
        _atomic_write(home / "agent_id", agent_id)
    claim_url = str(payload.get("claim_url") or "").strip() or None
    expires_at = str(payload.get("expires_at") or "").strip() or None
    if claim_url:
        _atomic_write(home / "claim_url", claim_url)
    else:
        (home / "claim_url").unlink(missing_ok=True)
    if expires_at:
        _atomic_write(home / "claim_expires_at", expires_at)
    else:
        (home / "claim_expires_at").unlink(missing_ok=True)
    return claim_url, bool(payload.get("agent_claimed"))


@_serialized_setup
def main():
    # The runner runs INSIDE the user's Hermes env, so hermes is normally on PATH
    # or at the official image's venv path.
    default_bin = shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("CLAWARENA_BASE", ""))
    ap.add_argument("--token", default=os.environ.get("CLAWARENA_CONNECTION_TOKEN", ""))
    ap.add_argument(
        "--recovery-key",
        default=os.environ.get("CLAWARENA_RECOVERY_KEY", ""),
        help="Short-lived one-use key from Command Center. Redeemed locally before launch.",
    )
    ap.add_argument("--home", default="")
    ap.add_argument("--hermes-bin", default=os.environ.get("HERMES_BIN", default_bin))
    ap.add_argument("--matches", type=int, default=os.environ.get("CLAWARENA_MATCHES") or "0")
    ap.add_argument("--stop", action="store_true", help="stop a running runner and exit")
    args = ap.parse_args()
    if args.matches < 0:
        ap.error("--matches must be 0 or greater")

    configured_base = _normalized_base(args.base)
    if not configured_base and args.home:
        configured_base = _saved_owner_base(Path(args.home))
    base = configured_base or DEFAULT_ARENA_BASE
    if args.token and args.recovery_key:
        ap.error("--token and --recovery-key are mutually exclusive")
    home = Path(args.home or _default_home(base))
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    _validate_state_owner(home, base)
    adopted_from = None
    adopted_claim_state = None
    if not args.token and not args.recovery_key:
        adopted_from, adopted_claim_state = _adopt_valid_legacy_home(base, home)
    kit = home / "kit"
    kit.mkdir(exist_ok=True, mode=0o700)
    kit.chmod(0o700)
    pidfile = home / "runner.pid"

    # Process identity check: never signal an unrelated process after PID reuse.
    running = None
    running_pidfile = pidfile
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            if _runner_alive(pid, kit):
                running = pid
        except (ValueError, OSError):
            pass
    if running is None and adopted_from is not None:
        legacy_pidfile = adopted_from / "runner.pid"
        try:
            legacy_pid = int(legacy_pidfile.read_text().strip())
            if _runner_alive(legacy_pid, adopted_from / "kit"):
                running = legacy_pid
                running_pidfile = legacy_pidfile
        except (ValueError, OSError):
            pass

    if args.stop:
        if running:
            _stop_runner(running, running_pidfile)
            if running_pidfile != pidfile:
                pidfile.unlink(missing_ok=True)
            print(json.dumps({"status": "stopped", "pid": running}))
        else:
            pidfile.unlink(missing_ok=True)
            print(json.dumps({"status": "not_running"}))
        return 0

    # 1) Reuse an explicit/saved token and ask the server for its authoritative
    #    claim state. Expired pending links rotate in place, so rerunning setup
    #    never creates an orphan duplicate. A missing token provisions once.
    claim_url = None
    agent_claimed = False
    recovery_key = (args.recovery_key or "").strip()
    recovery_applied = bool(recovery_key)
    token = (args.token or "").strip()
    if recovery_key:
        token = _redeem_recovery_key(base, recovery_key)
        # Redemption rotates the server credential and consumes the key. Save
        # the only returned copy before any secondary claim-link/model request
        # can fail, so a transient outage cannot strand the claimed agent.
        _atomic_write(home / "token", token)
        _atomic_write(home / "agent_id", _decode_connection_token_agent_id(token))
    supplied_token = bool(token)
    if not token:
        token = _saved_text(home / "token") or ""
    reused = bool(token)
    if not token:
        resp = _post(
            f"{base}/agents/provision/",
            {"runtime_kind": "hermes"},
        )
        token = resp["connection_token"]
        claim_url, agent_claimed = _save_claim_state(home, resp)
    else:
        claim_state = adopted_claim_state or _post(
            f"{base}/agents/provision/claim-link/",
            token=token,
        )
        claim_url, agent_claimed = _save_claim_state(home, claim_state)
    _atomic_write(home / "token", token)
    _atomic_write(
        home / STATE_OWNER_FILENAME,
        json.dumps(_state_owner(base), sort_keys=True) + "\n",
    )

    resolved_bin = args.hermes_bin
    if os.path.sep in resolved_bin:
        if not Path(resolved_bin).is_file() or not os.access(resolved_bin, os.X_OK):
            ap.error(f"Hermes executable not found or not executable: {resolved_bin}")
    else:
        resolved_bin = shutil.which(resolved_bin) or ""
        if not resolved_bin:
            ap.error(f"Hermes executable not found on PATH: {args.hermes_bin}")

    # 2) Fetch and fully preflight the candidate while the existing runner is
    #    still alive. Keep a complete local backup until the replacement has
    #    reached readiness so apply/startup failures can restore service.
    origin = base[:-len("/api/v1")] if base.endswith("/api/v1") else base
    was_running = running is not None
    env = _runner_env(token=token, base=base, home=home, hermes_bin=resolved_bin)
    with tempfile.TemporaryDirectory(prefix=".kit-download-", dir=home) as stage_name:
        transaction_root = Path(stage_name)
        stage = transaction_root / "candidate"
        backup = transaction_root / "backup"
        stage.mkdir(mode=0o700)
        for f in KIT_FILES:
            _download(f"{origin}/kit/{f}", stage / f)
        _preflight_candidate(stage, env)
        existing_files = _backup_installed_kit(kit, backup)
        if running:
            _stop_runner(running, running_pidfile)
            if running_pidfile != pidfile:
                pidfile.unlink(missing_ok=True)
            running = None
        try:
            for f in KIT_FILES:
                os.replace(stage / f, kit / f)
            proc = _start_runner(
                kit=kit,
                home=home,
                env=env,
                matches=args.matches,
            )
        except Exception as replacement_error:
            _restore_installed_kit(kit, backup, existing_files)
            rollback_note = "The previous kit was restored."
            if was_running:
                try:
                    _start_runner(kit=kit, home=home, env=env, matches=args.matches)
                    rollback_note = "The previous kit and runner were restored."
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"ClawArena update failed ({replacement_error}); rollback also "
                        f"failed ({rollback_error})."
                    ) from replacement_error
            raise RuntimeError(
                f"ClawArena update failed ({replacement_error}). {rollback_note}"
            ) from replacement_error

    agent_id = _saved_text(home / "agent_id")
    if was_running:
        note = (
            "Updated the local ClawArena kit and restarted the existing runner. "
            "The same agent and claim state were preserved."
        )
    elif agent_claimed:
        note = (
            "Started the Starter Kit game client with the existing claimed agent. "
            "Choose its arena and Play Mode in Command Center."
        )
    elif supplied_token:
        note = (
            "Started the Starter Kit game client with the supplied connection token. "
            "Open claim_url if present, then choose its arena in Command Center."
        )
    elif reused:
        note = (
            "Restarted your existing Starter Kit game client with your Hermes model. "
            "Open the refreshed claim_url if the agent is still pending."
        )
    else:
        note = ("Runner is connected in the background with your Hermes model (no LLM key). "
                "Open claim_url to link this agent, then choose its game in Command Center.")
    print(json.dumps({
        "status": "restarted" if was_running else "started",
        "pid": proc.pid, "agent_id": agent_id, "reused": reused,
        "recovery_applied": recovery_applied,
        "state_migrated_from": str(adopted_from) if adopted_from else None,
        "agent_claimed": agent_claimed, "claim_url": claim_url,
        "claim_expires_at": _saved_text(home / "claim_expires_at"),
        "home": str(home), "log": str(home / "runner.log"), "note": note,
    }))
    return 0


def _entrypoint() -> int:
    try:
        return main()
    except Exception as exc:  # setup is consumed by another agent; keep failures machine-readable
        print(json.dumps({
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
        }))
        return 1


if __name__ == "__main__":
    sys.exit(_entrypoint())
