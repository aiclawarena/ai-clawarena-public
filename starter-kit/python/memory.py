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
# Finished matches move to archive/ (input for post-match reflection and
# builder review sessions) instead of vanishing; pruned to the newest N.
ARCHIVE_DIR = MEMORY_DIR / "archive"
MAX_ARCHIVED = 20
MAX_MOVES = 60
MAX_MEMOS = 30

_current: dict = {"match_id": None, "data": None}
_lock = threading.RLock()


def _path(match_id) -> Path:
    return MEMORY_DIR / f"{match_id}.json"


def current_match_id():
    """Return the match whose memory is active in this runner process."""
    with _lock:
        return _current["match_id"]


def begin_turn(match_id, state: dict) -> dict:
    """Load (or start) this match's memory and return it for state injection."""
    with _lock:
        if _current["match_id"] != match_id or _current["data"] is None:
            data = {"my_role": None, "my_moves": [], "my_memos": []}
            path = _path(match_id)
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except Exception:  # noqa: BLE001 — corrupt memory is not worth a lost turn
                    pass
            _current.update(match_id=match_id, data=data)
        data = _current["data"]
        # The server projects the seat's role as state.my_role (mafia); accept the
        # older aliases too. Reading only your_role/role missed it entirely, so the
        # match memory never recorded the role it exists to keep consistent.
        role = state.get("my_role") or state.get("your_role") or state.get("role")
        if role and not data.get("my_role"):
            data["my_role"] = role
        return {
            "note": "This is YOUR OWN memory of this match — stay consistent with it.",
            "my_role": data.get("my_role"),
            "my_recent_moves": data.get("my_moves", [])[-MAX_MOVES:],
            "my_private_reads": data.get("my_memos", [])[-MAX_MEMOS:],
        }


def record_move(match_id, move: dict, phase=None) -> None:
    with _lock:
        if _current["match_id"] != match_id or _current["data"] is None:
            return
        entry = {"action": move.get("action"), "params": move.get("params"), "t": int(time.time())}
        if phase:
            entry["phase"] = phase
        moves = _current["data"].setdefault("my_moves", [])
        moves.append(entry)
        del moves[:-MAX_MOVES]
        _save()


def record_memo(match_id, memo: str | None) -> None:
    """Commit a model's private read only after its action is acknowledged."""
    with _lock:
        if _current["match_id"] != match_id or _current["data"] is None or not memo:
            return
        memos = _current["data"].setdefault("my_memos", [])
        memos.append(str(memo)[:200])
        del memos[:-MAX_MEMOS]
        _save()


def match_summary(match_id) -> dict | None:
    """Read-only snapshot for post-match reflection / review sessions."""
    if match_id is None:
        return None
    with _lock:
        for path in (_path(match_id), ARCHIVE_DIR / f"{match_id}.json"):
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except Exception:  # noqa: BLE001 — corrupt memory is not worth failing over
                    return None
                return {
                    "my_role": data.get("my_role"),
                    "my_moves": data.get("my_moves", [])[-20:],
                    "my_memos": data.get("my_memos", [])[-10:],
                }
    return None


def get_hermes_session(match_id=None):
    """The resumable `hermes chat` session id that has played this match so far.

    None until the match's first turn creates one. With no match_id it returns
    the CURRENT match's session (what decide() wants each turn); with an explicit
    match_id (post-match reflection) it reads the live file OR the archive, so a
    just-finished match's session is still resumable for self-learning.
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


def get_hermes_context_epoch(match_id=None):
    """Return the server-authored context epoch attached to a Hermes session."""
    with _lock:
        if _current["data"] is not None and (match_id is None or match_id == _current["match_id"]):
            return _current["data"].get("hermes_context_epoch")
        if match_id is None:
            return None
        for path in (_path(match_id), ARCHIVE_DIR / f"{match_id}.json"):
            if path.exists():
                try:
                    return json.loads(path.read_text()).get("hermes_context_epoch")
                except Exception:  # noqa: BLE001
                    return None
    return None


def set_hermes_session(sid) -> None:
    """Persist the current Hermes match session."""
    with _lock:
        if _current["data"] is None or not sid or _current["data"].get("hermes_session") == sid:
            return
        _current["data"]["hermes_session"] = sid
        _current["data"]["hermes_session_turn_count"] = 0
        _save()


def set_hermes_context_epoch(epoch) -> None:
    """Persist the server boundary used by the current Hermes session."""
    value = str(epoch or "").strip()
    with _lock:
        if _current["data"] is None:
            return
        if value:
            if _current["data"].get("hermes_context_epoch") == value:
                return
            _current["data"]["hermes_context_epoch"] = value
        else:
            if "hermes_context_epoch" not in _current["data"]:
                return
            _current["data"].pop("hermes_context_epoch", None)
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
        if _current["data"] is None or not any(
            key in _current["data"]
            for key in ("hermes_session", "hermes_context_epoch")
        ):
            return
        _current["data"].pop("hermes_session", None)
        _current["data"].pop("hermes_session_turn_count", None)
        _current["data"].pop("hermes_context_epoch", None)
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
