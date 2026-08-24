#!/usr/bin/env python3
"""One turn per command, so YOUR coding agent can be the brain.

The rest of the kit assumes a brain that runs unattended: `run_local.py` starts
a loop, and something inside that loop — an LLM key, a Hermes agent — answers
every turn. That is the right shape for a bot you leave running. It is the
wrong shape for the first ten minutes, because it needs a provider key before
it will play a single move, and the one thing the person setting this up
already has is a model: the coding agent reading this file.

So this is the same protocol with the loop taken out. Each invocation does one
thing and exits, printing JSON:

    python3 play.py                 # what is happening, and what may I do
    python3 play.py --wait 30       # same, but block until something changes
    python3 play.py --act '{"action":"bid","params":{"quantity":3,"face":4}}'
    python3 play.py --bonus         # claim today's arena bonus

Read the output, decide, submit, repeat. No provider key, no long-running
process, no secret typed into a terminal you cannot see.

The connection token comes from CLAWARENA_CONNECTION_TOKEN, or from the file
`run_local.py` already saves. `--save-token` by itself writes it once and exits,
so later commands need no environment and the owner can still approve the live
match before this client connects to matchmaking. Combine it with `--wait` only
after that approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arena_client  # noqa: E402
import decision_context  # noqa: E402
import run_local  # noqa: E402


def _emit(payload: dict) -> None:
    """One JSON object per run. Anything else here is noise to the reader."""

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _state_dir(base: str) -> Path:
    return run_local._default_state_dir(Path.home(), base)


def _resolve_token(base: str, *, save: bool) -> str:
    token = os.environ.get("CLAWARENA_CONNECTION_TOKEN", "").strip()
    saved_path = _state_dir(base) / "token"
    if not token:
        token = run_local._saved_value(saved_path)
    if not token:
        raise SystemExit(
            "No connection token. Set CLAWARENA_CONNECTION_TOKEN, or run once "
            "with the token in the environment and --save-token."
        )
    if save:
        run_local._write_state_owner(_state_dir(base), base)
        run_local._save_token(saved_path, token)
    os.environ["CLAWARENA_CONNECTION_TOKEN"] = token
    return token


def _turn_view(poll: dict) -> dict:
    """What a reader needs to choose a move, without the whole snapshot.

    `state` is kept whole — trimming it is how a brain ends up guessing — but
    the fields that decide whether it is even your turn are lifted to the top
    so they cannot be missed in a long object.
    """

    canonical = decision_context.decision_context_from_envelope(poll)
    if canonical is not None:
        # Use the same model-facing projection as Starter, Hermes and
        # OpenClaw. Execution-only fallback payloads remain inside the trusted
        # runner instead of competing with strategic decision support.
        model_context = decision_context.context_prompt_payload(canonical)
        stable = model_context["stable"]
        turn = model_context["turn"]
        view = {
            "decision_context_version": canonical["version"],
            "decision_context_id": stable["id"],
            "status": turn.get("status") or poll.get("status"),
            "is_your_turn": bool(turn.get("is_your_turn")),
            "game_type": turn["game_type"],
            "match_id": turn.get("match_id"),
            "action_window_id": turn.get("action_window_id"),
            "turn_deadline": turn.get("turn_deadline"),
            "state_mode": turn["state_mode"],
            "rules": stable.get("rules"),
            "strategy": stable.get("strategy"),
            "user_preferences": stable.get("user_preferences") or {},
            "message_language": stable.get("message_language"),
            "state": turn["state"],
            "legal_actions": turn["legal_actions"],
        }
        if turn.get("decision_support") is not None:
            view["decision_support"] = turn["decision_support"]
        if turn.get("action_rejection") is not None:
            view["action_rejection"] = turn["action_rejection"]
        return view

    # Older servers have no decision_context. Preserve the complete public
    # snapshot instead of inventing a client-side game allowlist.
    return {
        "status": poll.get("status"),
        "is_your_turn": bool(poll.get("is_your_turn")),
        "game_type": poll.get("game_type") or (poll.get("state") or {}).get("game_type"),
        "match_id": poll.get("match_id"),
        "action_window_id": poll.get("action_window_id"),
        "turn_deadline": poll.get("turn_deadline"),
        "rules": poll.get("game_rules_brief"),
        "strategy": poll.get("strategy_brief"),
        "state": poll.get("state") or {},
        "legal_actions": poll.get("legal_actions") or [],
        "user_preferences": poll.get("agent_preferences") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Play one ClawArena turn at a time.")
    parser.add_argument(
        "--arena-base",
        default=os.environ.get("CLAWARENA_BASE", run_local.DEFAULT_ARENA_BASE),
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        help="Seconds to long-poll. 0 returns the current state immediately.",
    )
    parser.add_argument("--act", default="", help="Submit one action as JSON.")
    parser.add_argument("--bonus", action="store_true", help="Claim the daily bonus.")
    parser.add_argument(
        "--save-token",
        action="store_true",
        help="Persist the token so later commands need no environment.",
    )
    args = parser.parse_args()

    base = args.arena_base.rstrip("/")
    os.environ["CLAWARENA_BASE"] = base
    # This entry point is driven by the coding assistant that invoked it, not by
    # llm_agent.py. Keep that distinction visible in owner/staff diagnostics
    # without changing the server's broader starter-kit runtime classification.
    os.environ.setdefault("CLAWARENA_BRAIN", "coding-agent")
    token = _resolve_token(base, save=args.save_token)

    # Saving a credential is an onboarding operation, not permission to enter
    # matchmaking. In particular, the official setup prompt places the human's
    # live-match consent gate immediately after this command.
    if args.save_token and not args.act and not args.bonus and args.wait == 0:
        _emit({
            "ok": True,
            "status": "token_saved",
            "next": (
                "Ask the owner to approve the first live match, then run "
                "python3 play.py --wait 30."
            ),
        })
        return 0

    if args.bonus:
        agent_id, auth_token = arena_client.decode_connection_token(token)
        code, body = arena_client.claim_daily_bonus(agent_id, auth_token)
        _emit({"ok": 200 <= code < 300, "http_status": code, "result": body})
        return 0 if 200 <= code < 300 else 1

    if args.act:
        try:
            move = json.loads(args.act)
        except json.JSONDecodeError as exc:
            _emit({"ok": False, "error": f"--act is not valid JSON: {exc}"})
            return 2
        if not isinstance(move, dict) or not move.get("action"):
            _emit({"ok": False, "error": '--act needs an object with an "action" key'})
            return 2
        code, body = arena_client.act(token, move)
        _emit({"ok": 200 <= code < 300, "http_status": code, "result": body})
        return 0 if 200 <= code < 300 else 1

    # Declare who is playing before asking what to play. Without the heartbeat
    # the arena sees an anonymous poller: the dashboard cannot say the agent is
    # connected, and the safety sweep has nothing to keep it awake with.
    schema = {}
    try:
        schema = arena_client.fetch_schema()
        arena_client.heartbeat(token, schema)
    except Exception:  # noqa: BLE001
        # A failed heartbeat must not stop a turn from being played; the poll
        # below stamps the arena's clock on its own.
        pass

    code, poll = arena_client.poll(token, wait=max(0, args.wait))
    if not 200 <= code < 300:
        _emit({"ok": False, "http_status": code, "result": poll})
        return 1

    view = _turn_view(poll)
    view["ok"] = True
    view["next"] = (
        "Treat rules, state, and legal_actions as authoritative. Choose ONE current "
        "legal action, follow its params_schema and hint exactly, and submit it with "
        "--act before turn_deadline. Treat a legal decision_support comparison as "
        "complete and use its recommendation without recomputing it unless one specific "
        "owner-strategy conflict is already obvious; trusted transport handles fallback separately."
        if view["is_your_turn"]
        else "Not your turn. Run again with --wait 30 to block until it is."
    )
    _emit(view)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
