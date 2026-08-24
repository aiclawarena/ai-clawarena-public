"""Materialize the server's decision-context board from a delta stream.

The server can send each turn's board either whole (``state_mode="full"``) or as
a top-level diff against the board the client already holds
(``state_mode="delta"``). This module owns that reconstruction, and it lives at
the TRANSPORT boundary on purpose: everything above it -- the runner, the brain,
the prompt builders -- keeps receiving one complete board per turn exactly as it
does today. Nothing above here needs to know which mode the wire used.

That placement is not a convenience. Applying deltas any higher would put a diff
in front of a prompt whose scaffold says "obey turn.state", and would strand the
resync decision in the brain, which has no transport to ask.

Recovery has two independent mechanisms and this module needs both:

* ``state_seq`` / ``state_ack`` -- the client tells the server which board it
  actually holds, so a response that was lost is made good with one exact delta
  rather than leaving the client permanently behind.
* ``state_checksum`` -- proves the two sides agree on the RESULT. Sequence
  numbers only prove nothing was skipped; they cannot catch a delta that was
  merged differently at each end.

When either says the board cannot be trusted, this module refuses to guess and
asks for a fresh baseline.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping

APPEND_KEY = "_appended"
LITERAL_KEY = "_literal"


def checksum(board: Mapping) -> str:
    """Fingerprint a complete board. Must match the server's derivation."""

    canonical = json.dumps(
        board, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


class DeltaError(Exception):
    """The delta cannot be applied to the board we hold."""


def apply_delta(board: Mapping, delta: Mapping, removed=()) -> dict:
    """Return a new complete board with ``delta`` applied.

    Three value shapes are recognised, and anything else is a replacement:

        {"_appended": [...]}  extend the list we already hold
        {"_literal": <any>}   use verbatim; the server escaped a value that
                              would otherwise have read as an instruction
        <any other value>     replace

    An append against a key we do not hold, or hold as a non-list, is a real
    divergence rather than something to paper over -- it means our board is not
    the one the server diffed against -- so it raises.
    """

    merged = copy.deepcopy(dict(board))
    for key, value in delta.items():
        if isinstance(value, Mapping) and LITERAL_KEY in value and len(value) == 1:
            merged[key] = copy.deepcopy(value[LITERAL_KEY])
            continue
        if isinstance(value, Mapping) and APPEND_KEY in value and len(value) == 1:
            held = merged.get(key)
            if not isinstance(held, list):
                raise DeltaError(
                    f"append to {key!r} but the held value is {type(held).__name__}"
                )
            addition = value[APPEND_KEY]
            if not isinstance(addition, list):
                raise DeltaError(f"append to {key!r} carried a non-list payload")
            merged[key] = held + copy.deepcopy(addition)
            continue
        merged[key] = copy.deepcopy(value)
    for key in removed or ():
        merged.pop(str(key), None)
    return merged


class MatchState:
    """The board this client holds for one match, and how far it has applied.

    One instance per (match, game). Rebuilt from scratch whenever the identity
    changes or the stream cannot be trusted, because a stale board is worse than
    no board: it is wrong silently.
    """

    def __init__(self) -> None:
        self.key: tuple | None = None
        self.board: dict | None = None
        self.applied_seq: int = 0
        self.last_error: str = ""

    # -- transport interface ------------------------------------------------

    def ack(self) -> int | None:
        """The seq to send as ``state_ack``, if we hold a board at all.

        No match argument: the caller cannot know which match the next poll will
        answer with. An ack from a different match is harmless — the server looks
        it up in that match's own history, misses, and answers with a full
        baseline, which is the correct response to an ack it cannot honour.
        """

        if self.board is None:
            return None
        return self.applied_seq or None

    def reset(self, reason: str = "") -> None:
        self.key = None
        self.board = None
        self.applied_seq = 0
        self.last_error = reason

    def ingest(self, turn: Mapping, *, match_id, game_type) -> dict | None:
        """Fold one turn into the held board and return the complete board.

        Returns ``None`` when a fresh baseline is required; the caller re-polls
        with a resync. ``last_error`` says why, for the log line.
        """

        key = (match_id, game_type)
        if self.key != key:
            self.reset()
            self.key = key

        mode = str(turn.get("state_mode") or "full").strip().lower()
        state = turn.get("state")
        state = dict(state) if isinstance(state, Mapping) else {}
        seq = _positive_int(turn.get("state_seq"))
        expected = str(turn.get("state_checksum") or "")

        if mode == "full":
            board = copy.deepcopy(state)
        elif self.board is None:
            # A delta arrived first. That happens when the server's cursor
            # outlives this process -- a restart, a recreate, a new worker --
            # and it is exactly the case that must not be papered over: there is
            # no base to apply it to.
            self.last_error = "delta arrived with no baseline held"
            return None
        else:
            try:
                board = apply_delta(self.board, state, turn.get("state_removed") or ())
            except DeltaError as exc:
                self.last_error = f"delta rejected: {exc}"
                self.reset(self.last_error)
                return None

        if expected and checksum(board) != expected:
            # Both sides advanced but disagree on the result. Nothing local can
            # repair that; only a fresh baseline can.
            self.last_error = (
                f"board checksum {checksum(board)} != server {expected}"
            )
            self.reset(self.last_error)
            return None

        self.key = key
        self.board = board
        self.applied_seq = seq or self.applied_seq
        self.last_error = ""
        return copy.deepcopy(board)


def _positive_int(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
