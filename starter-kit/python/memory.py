"""Per-match memory — compact, restart-proof session continuity.

Mafia is a consistency game: your own past claims, votes, and reads must not
contradict. Stateless kit brains use this directly; Hermes also carries it across
native context compaction and explicit recovery sessions. Unlike a provider
transcript, it survives restarts and never gets compacted away.

The runner drives the lifecycle and commits the model's optional one-line
"memo" only after the corresponding action is acknowledged.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

# Overridable so offline tools (mock_arena) never touch your real match record.
MEMORY_DIR = Path(os.environ.get("CLAWARENA_KIT_MEMORY_DIR")
                  or Path.home() / ".clawarena" / "kit-memory")
# Finished matches move to archive/ for owner/builder review instead of
# vanishing; pruned to the newest N.
ARCHIVE_DIR = MEMORY_DIR / "archive"
MAX_ARCHIVED = 20

_current: dict = {"match_id": None, "data": None}
_lock = threading.RLock()


def _path(match_id) -> Path:
    return MEMORY_DIR / f"{match_id}.json"


def current_match_id():
    """Return the match whose memory is active in this runner process."""
    with _lock:
        return _current["match_id"]


def open_match(match_id) -> None:
    """Make this the active match record. Call once per turn; idempotent.

    This used to be ``begin_turn`` and used to return a ``my_memory`` block --
    the seat's own move and memo log -- for injection into the prompt. That log
    is gone: an accumulating session already holds every earlier turn verbatim,
    and where it does not, the compaction note is what carries the match
    forward. Sending the log alongside either one only duplicated it.

    What remains is load-bearing and unrelated to prompts: this is the only
    writer of ``_current``, which ``current_match_id`` reads for session
    identity and the Hermes helpers read for the resumable session id.
    """
    with _lock:
        if _current["match_id"] != match_id or _current["data"] is None:
            data = {}
            path = _path(match_id)
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except Exception:  # noqa: BLE001 — corrupt memory is not worth a lost turn
                    data = {}
            _current.update(match_id=match_id, data=data)





def get_hermes_session(match_id=None):
    """The resumable `hermes chat` session id that has played this match so far.

    None until the match's first turn creates one. With no match_id it returns
    the CURRENT match's session (what decide() wants each turn); with an explicit
    match_id it reads the live file OR the archive for diagnostics.
    """
    with _lock:
        if _current["data"] is not None and (match_id is None or match_id == _current["match_id"]):
            return _current["data"].get("hermes_session")
        if match_id is None:
            return None
        for path in (_path(match_id), ARCHIVE_DIR / f"{match_id}.json"):
            if path.exists():
                try:
                    return json.loads(path.read_text()).get("hermes_session")
                except Exception:  # noqa: BLE001 — corrupt memory is not worth failing over
                    return None
    return None


def get_hermes_session_turn_count(match_id=None) -> int:
    """Return how many decisions the current Hermes session has accumulated."""
    with _lock:
        if _current["data"] is not None and (match_id is None or match_id == _current["match_id"]):
            value = _current["data"].get("hermes_session_turn_count", 0)
        elif match_id is not None:
            value = 0
            for path in (_path(match_id), ARCHIVE_DIR / f"{match_id}.json"):
                if path.exists():
                    try:
                        value = json.loads(path.read_text()).get("hermes_session_turn_count", 0)
                    except Exception:  # noqa: BLE001
                        value = 0
                    break
        else:
            value = 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


def set_hermes_session(sid) -> None:
    """Persist the current Hermes match session."""
    with _lock:
        if _current["data"] is None or not sid or _current["data"].get("hermes_session") == sid:
            return
        _current["data"]["hermes_session"] = sid
        _current["data"]["hermes_session_turn_count"] = 0
        _save()


def set_hermes_session_turn_count(turn_count: int) -> None:
    """Persist resumable-session progress across container restarts."""
    with _lock:
        if _current["data"] is None or not _current["data"].get("hermes_session"):
            return
        _current["data"]["hermes_session_turn_count"] = max(0, int(turn_count))
        _save()


def clear_hermes_session() -> None:
    """Forget a stale CURRENT match session so Hermes can recreate it."""
    with _lock:
        if _current["data"] is None or "hermes_session" not in _current["data"]:
            return
        _current["data"].pop("hermes_session", None)
        _current["data"].pop("hermes_session_turn_count", None)
        _save()


def end_match(match_id) -> None:
    with _lock:
        try:
            path = _path(match_id)
            if path.exists():
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.replace(ARCHIVE_DIR / f"{match_id}.json")
                archived = sorted(ARCHIVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
                for stale in archived[:-MAX_ARCHIVED]:
                    stale.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        if _current["match_id"] == match_id:
            _current.update(match_id=None, data=None)


def _save() -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        MEMORY_DIR.chmod(0o700)
        target = _path(_current["match_id"])
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=MEMORY_DIR)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_current["data"], handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except Exception:  # noqa: BLE001 — memory is best-effort, never lose a turn to it
        pass
