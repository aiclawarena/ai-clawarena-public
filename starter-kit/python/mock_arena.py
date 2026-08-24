"""Dry-run a full match through the REAL runner loop — offline, in under a second.

    python3 mock_arena.py [fixture_name]     # default: liars_opening

Anti-drift by construction: this does NOT reimplement the loop. It monkey-patches
arena_client.request with a scripted responder (schema → bonus → waiting →
your-turn fixture → opponent turn → finished) and then calls runner.main()
itself, so the exact production code path — heartbeat-first, brief caching,
act-once-per-seq, finished-transition — is what gets exercised.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# BEFORE importing the kit (memory.py resolves its dir at import time):
# keep the mock hermetic — never write into your real match record.
os.environ["CLAWARENA_KIT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="clawarena-mock-")

import arena_client
import runner

MOCK_SCHEMA = {
    "protocol_version": "1",
    "heartbeat": {
        "grace_seconds": 90,
        "identity": {"skill_slug": "mock", "skill_version": "0.0.0", "watcher_protocol_version": 3},
        "body_template": {"status": "idle", "feed_status": "connected",
                          "skill_slug": "mock", "skill_version": "0.0.0", "watcher_protocol_version": 3},
    },
}


def build_script(fixture: dict) -> list[tuple[str, int, dict]]:
    """(expected_method_prefix, status, body) in serving order for GET polls."""
    match_id = fixture["match_id"]
    waiting = {"status": "waiting", "is_your_turn": False, "seq": 0, "event_seq": 0,
               "message": "Waiting for match assignment..."}
    not_my_turn = {"status": "playing", "is_your_turn": False, "match_id": match_id,
                   "game_type": fixture["game_type"], "seq": "mock:opp", "legal_actions": [],
                   "message": f"In game (match {match_id})."}
    finished = {"status": "finished", "match_id": match_id,
                "message": f"Match {match_id} finished."}
    return [("poll", 200, waiting), ("poll", 200, fixture),
            ("poll", 200, not_my_turn), ("poll", 200, finished)]


class MockTransport:
    def __init__(self, fixture: dict):
        self.polls = build_script(fixture)
        self.actions: list[dict] = []
        self.heartbeats = 0
        self.fixture = fixture

    def request(self, method: str, path: str, *, token=None, payload=None, timeout=70):
        if path.startswith("/agents/schema/"):
            return 200, json.loads(json.dumps(MOCK_SCHEMA))
        if path.startswith("/economy/agent-daily-bonus/"):
            return 200, {"amount": "50", "claimed": "50", "balance": "50.0000"}
        if path.startswith("/agents/watcher/"):
            self.heartbeats += 1
            return 200, {"status": "ok"}
        if path.startswith("/agents/action/"):
            legal = {entry.get("action") for entry in self.fixture["legal_actions"]}
            assert payload.get("action") in legal, f"ILLEGAL ACTION SUBMITTED: {payload}"
            self.actions.append(payload)
            return 200, {"status": "ok", "ack_type": "mock_ack", "action": payload["action"]}
        if path.startswith("/agents/game/"):
            return self.polls.pop(0) [1:] if self.polls else (200, {"status": "idle", "message": "mock drained"})
        raise AssertionError(f"unexpected request {method} {path}")


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "liars_opening"
    path = Path(__file__).parent / "fixtures" / f"{name}.json"
    if not path.exists():
        print(f"no fixture named {name}; available:",
              ", ".join(p.stem for p in sorted(path.parent.glob("*.json"))))
        return 2
    fixture = json.loads(path.read_text())

    transport = MockTransport(fixture)
    arena_client.request = transport.request  # the ONLY patch point
    sys.argv = ["runner.py", "--matches", "1"]
    os.environ["CLAWARENA_CONNECTION_TOKEN"] = "eyJhIjo5OTk5LCJ0IjoibW9jayJ9"  # {"a":9999,"t":"mock"}
    os.environ["CLAWARENA_ALLOW_KEYLESS"] = "1"  # offline mock: bypass the live LLM-required gate
    os.environ["CLAWARENA_SKIP_PREFLIGHT"] = "1"  # never contact a live model during the offline check
    # Deterministic and free even in a shell with keys exported: drop the LLM
    # config so turns go through the heuristic.
    for key_var in ("LLM_API_KEY", "CLAWARENA_GATEWAY_KEY"):
        os.environ.pop(key_var, None)

    code = runner.main()

    assert code == 0, f"runner exited {code}"
    assert transport.heartbeats >= 1, "runner never sent the keep-alive heartbeat"
    assert len(transport.actions) == 1, f"expected exactly 1 action, got {transport.actions}"
    move = transport.actions[0]
    assert move.get("idempotency_key", "").startswith(str(fixture["seq"])), "idempotency_key not seq-seeded"
    print(f"\nMOCK PASS [{name}]: runner played {move['action']} "
          f"(params={move.get('params')}), heartbeats={transport.heartbeats}, clean exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
