"""The poll -> decide -> act loop. You should not need to edit this file.

Implements the verified client discipline from docs/agent-api-v1.md §7:
schema bootstrap (fail loud on drift), snapshot=full stateless polling,
heartbeat + identity while queueing, decide once per stable action window, treat an
"already queued" 409 after our own 200 as success, respect turn deadlines.

Usage:
    CLAWARENA_CONNECTION_TOKEN=... python3 runner.py [--dry-run] [--matches N]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import arena_client
import agent as heuristic_agent
import match_state
import decision_context as decision_context_contract
import helpers
import memory

try:
    from decision_policy import (
        DEFAULT_DECISION_CAP_SECONDS,
        DIPLOMACY_DECISION_CAP_SECONDS,
        DEFAULT_SUBMIT_RESERVE_SECONDS,
        HERMES_SUBMIT_RESERVE_SECONDS,
        decision_budget as shared_decision_budget,
        decision_cap_seconds as shared_decision_cap_seconds,
    )
    _MANAGED_DECISION_POLICY = True
except ModuleNotFoundError:
    # The public Builder Starter Kit intentionally omits decision_policy.py.
    # That absence is the policy boundary: a BYO client receives the server
    # deadline but does not inherit our hosted fleet's 105s/165s inference cap.
    # Builders can opt into their own cap/reserve through the documented env
    # values. Managed Starter/Hermes images and OpenClaw still ship/import the
    # shared policy above and therefore keep their managed safety contract.
    _MANAGED_DECISION_POLICY = False
    DEFAULT_DECISION_CAP_SECONDS = None
    DIPLOMACY_DECISION_CAP_SECONDS = None
    DEFAULT_SUBMIT_RESERVE_SECONDS = 0.0
    HERMES_SUBMIT_RESERVE_SECONDS = 0.0

    def shared_decision_cap_seconds(envelope: dict) -> float | None:
        state = envelope.get("state") if isinstance(envelope.get("state"), dict) else {}
        game_type = envelope.get("game_type") or state.get("game_type")
        is_diplomacy = str(game_type or "").strip().lower() == "diplomacy"
        raw = (
            os.environ.get("CLAWARENA_DIPLOMACY_DECISION_MAX_SECONDS")
            if is_diplomacy
            else None
        )
        if raw is None:
            raw = os.environ.get("CLAWARENA_DECISION_MAX_SECONDS")
        if raw is None or not str(raw).strip():
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def shared_decision_budget(
        envelope: dict,
        *,
        configured_seconds: float | None = None,
        submit_reserve_seconds: float = DEFAULT_SUBMIT_RESERVE_SECONDS,
        clock=time.time,
    ) -> dict:
        selected_cap = (
            shared_decision_cap_seconds(envelope)
            if configured_seconds is None
            else configured_seconds
        )
        configured = (
            max(0.0, float(selected_cap))
            if selected_cap is not None
            else None
        )
        reserve = max(0.0, float(submit_reserve_seconds))
        raw = envelope.get("turn_deadline")
        state = envelope.get("state") if isinstance(envelope.get("state"), dict) else {}
        raw = raw or state.get("turn_deadline")
        if not raw:
            return {
                "configured_seconds": configured,
                "effective_seconds": configured or 0.0,
                "server_remaining_seconds": (configured or 0.0) + reserve,
                "submit_reserve_seconds": reserve,
                "policy": (
                    "configured_cap_no_deadline"
                    if configured is not None
                    else "server_deadline_missing"
                ),
            }
        try:
            deadline = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            return {
                "configured_seconds": configured,
                "effective_seconds": configured or 0.0,
                "server_remaining_seconds": (configured or 0.0) + reserve,
                "submit_reserve_seconds": reserve,
                "policy": (
                    "configured_cap_invalid_deadline"
                    if configured is not None
                    else "server_deadline_invalid"
                ),
            }
        remaining = max(0.0, deadline - clock())
        available = max(0.0, remaining - reserve)
        return {
            "configured_seconds": configured,
            "effective_seconds": (
                min(configured, available)
                if configured is not None
                else available
            ),
            "server_remaining_seconds": remaining,
            "submit_reserve_seconds": reserve,
            "policy": (
                "deadline_submit_reserve"
                if configured is not None
                else (
                    "server_deadline_reserve"
                    if reserve > 0
                    else "server_deadline_only"
                )
            ),
        }

# The unattended runner uses an OpenAI-compatible provider key by default and
# falls back to agent.py only after a failed live decision. A coding assistant
# such as Codex needs no provider key because it uses play.py one turn at a
# time, not this process. CLAWARENA_BRAIN=hermes routes unattended decisions
# through the user's existing Hermes model instead.
if os.environ.get("CLAWARENA_BRAIN", "").strip().lower() == "hermes":
    os.environ.setdefault("CLAWARENA_ALLOW_KEYLESS", "1")
    import hermes_agent as brain

    # A kept session and the server's delta transport are one design, not two
    # options. The transcript is what makes the server's rolling truncation
    # harmless -- each entry arrives once, when it is new, and stays -- and the
    # delta is what stops us re-sending a board the session already holds.
    # Running the session on whole boards would append the same state to the
    # transcript every turn, which is the shape this whole programme spent its
    # time removing. The OpenClaw watcher already asks for `session`
    # unconditionally, so this brings the harnesses onto one wire rather than
    # inventing a third.
    #
    # Decided HERE, where the brain is actually chosen, and not at import in
    # hermes_agent: CLAWARENA_DELTA_TRANSPORT is read by arena_client and shared
    # by every brain, so a module that flips it on import would change the wire
    # for the starter brain too, in any process that merely imports it.
    # setdefault leaves an explicit operator setting alone.
    if not brain.HERMES_STATELESS_GAMEPLAY:
        os.environ.setdefault("CLAWARENA_DELTA_TRANSPORT", "1")
else:
    import llm_agent as brain


# Equal jitter sleeps in [ceiling / 2, ceiling]. Doubling the previous fixed
# delays preserves their minimum while spreading each retry cohort over time.
POLL_RETRY_BASE_SECONDS = 6.0
POLL_RATE_LIMIT_RETRY_BASE_SECONDS = 20.0
POLL_RETRY_MAX_SECONDS = 30.0
# Hosted clients import decision_policy.py and keep the managed 105s/165s caps.
# A downloaded Builder Kit intentionally has no such module: its only default
# boundary is the server-authored turn_deadline, visible in every decision
# context. Optional BYO caps and submit reserve are owner configuration.
def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _queue_status_message(poll: dict) -> str:
    """Explain an arena hold without blaming queue supply.

    Operation mode is additive to the stable ``status`` vocabulary, so old
    clients keep working and this runner remains connected through a deploy.
    """
    matchmaking = poll.get("matchmaking")
    if (
        isinstance(matchmaking, dict)
        and matchmaking.get("accepting_new_matches") is False
    ):
        message = str(
            matchmaking.get("message")
            or matchmaking.get("public_message")
            or ""
        ).strip()
        if message:
            return message
        if matchmaking.get("error"):
            return (
                "Arena matchmaking status is temporarily unavailable. "
                "New matches are paused safely; the runner will keep polling."
            )
        return (
            "Arena update in progress. New matches are temporarily paused; "
            "the runner will keep polling."
        )
    return str(poll.get("message") or poll.get("status") or "idle")


def _decision_budget(poll: dict, *, brain_kind: str | None = None) -> dict:
    """Use managed policy when installed; otherwise expose the server window."""
    selected_brain = (
        brain_kind
        if brain_kind is not None
        else os.environ.get("CLAWARENA_BRAIN", "")
    )
    is_hermes = str(selected_brain).strip().lower() == "hermes"
    if _MANAGED_DECISION_POLICY:
        reserve = (
            HERMES_SUBMIT_RESERVE_SECONDS
            if is_hermes
            else DEFAULT_SUBMIT_RESERVE_SECONDS
        )
    else:
        try:
            reserve = max(
                0.0,
                float(os.environ.get("CLAWARENA_SUBMIT_RESERVE_SECONDS", "0")),
            )
        except (TypeError, ValueError):
            reserve = 0.0
    return shared_decision_budget(
        poll,
        configured_seconds=shared_decision_cap_seconds(poll),
        submit_reserve_seconds=reserve,
        clock=time.time,
    )


def _round_optional_seconds(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _decision_budget_seconds(poll: dict) -> float:
    """Backward-compatible scalar accessor used by focused callers/tests."""
    return float(_decision_budget(poll)["effective_seconds"])


def _decision_context_for_turn(
    poll: dict,
    state: dict,
    legal_actions: list[dict],
    action_rejection: dict | None = None,
) -> dict | None:
    """Return the detached server context, filling only absent v1 fields.

    Old v1 servers sometimes omit one-shot stable fields after their first
    delivery.  The runner's match cache is only a compatibility backstop: it
    must never replace a value that the server included in the context itself.
    """
    envelope = dict(poll)
    if state.get("game_rules_brief") is not None:
        envelope.setdefault("game_rules_brief", state["game_rules_brief"])
    if state.get("strategy_brief") is not None:
        envelope.setdefault("strategy_brief", state["strategy_brief"])
    if state.get("user_preferences") is not None:
        envelope.setdefault("agent_preferences", state["user_preferences"])
    canonical = decision_context_contract.decision_context_from_envelope(envelope)
    if canonical is None:
        return None
    server_actions = {
        entry.get("action"): entry
        for entry in canonical["turn"]["legal_actions"]
        if isinstance(entry, dict) and entry.get("action")
    }
    canonical["turn"]["legal_actions"] = [
        copy.deepcopy(server_actions.get(entry.get("action"), entry))
        for entry in legal_actions
        if isinstance(entry, dict) and entry.get("action")
    ]
    if action_rejection:
        canonical["turn"]["action_rejection"] = copy.deepcopy(action_rejection)
    return canonical


def _server_fallback(state: dict, legal_actions: list[dict]) -> dict | None:
    return decision_context_contract.executable_fallback(
        state.get("_decision_context"),
        legal_actions,
    )


def _action_span(action_window_id, match_id, game_type, stage, started, **extra) -> None:
    payload = {
        "event": "clawarena_action_span",
        "action_window_id": str(action_window_id or ""),
        "match_id": match_id,
        "game_type": game_type,
        "stage": stage,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


_NEEDS_RESYNC = object()


def _materialize_board(board_state, poll):
    """Fold a delta turn back into a complete board, in place.

    Everything above the transport reads ``decision_context.turn.state`` as the
    whole board, so this rewrites the response to say exactly that before it is
    seen. Returns the board, or ``_NEEDS_RESYNC`` when it cannot be trusted.
    """

    context = poll.get("decision_context")
    if not isinstance(context, dict):
        return None
    turn = context.get("turn")
    if not isinstance(turn, dict):
        return None
    complete = board_state.ingest(
        turn,
        match_id=poll.get("match_id"),
        game_type=turn.get("game_type") or poll.get("game_type"),
    )
    if complete is None:
        return _NEEDS_RESYNC
    turn["state"] = complete
    # The wire mode was an implementation detail of the transport. Above here a
    # turn is always a complete board, which is what every consumer already
    # assumes and what keeps the bounded prompt path honest.
    turn["state_mode"] = "full"
    turn["state_removed"] = []
    return complete


def _poll_retry_delay(
    failure_count: int,
    status_code: int,
    *,
    rng=None,
) -> float:
    """Bound poll recovery with equal jitter so a fleet does not retry in lockstep."""
    base = (
        POLL_RATE_LIMIT_RETRY_BASE_SECONDS
        if status_code == 429
        else POLL_RETRY_BASE_SECONDS
    )
    exponent = min(10, max(0, int(failure_count) - 1))
    ceiling = min(
        POLL_RETRY_MAX_SECONDS,
        base * (2 ** exponent),
    )
    random_value = (rng or random.random)()
    return (ceiling / 2.0) + (max(0.0, min(1.0, random_value)) * ceiling / 2.0)


def _is_transient_action_response(status_code: int, result: dict) -> bool:
    """Return whether an action may be safely replayed with the same key.

    Agent API actions never intentionally redirect. During a core-stack
    replacement Traefik can briefly lose the API service and route the same
    path to the web fallback, which answers 3xx HTML without touching game
    state. Treat that like an unconfirmed 0/5xx ACK, alongside the explicit
    turn-lock update signal, and preserve the already-paid model decision.
    """
    if status_code == 0 or status_code == 429 or status_code >= 500:
        return True
    if 300 <= status_code < 400:
        return True
    return status_code == 409 and (
        str(result.get("code") or "").strip().lower() == "turn_updating"
        or "updat" in str(result.get("message") or "").lower()
    )


def _should_deliver(poll: dict) -> bool:
    """Per-turn report gate — mirrors the OpenClaw watcher exactly. The server
    puts the dashboard `report_level` (silent | important_only | every_turn) in
    agent_preferences and a per-turn `report_important` flag at the top level of
    every poll, so any runtime reports on the same turns OpenClaw would."""
    prefs = poll.get("agent_preferences") or {}
    level = str(prefs.get("report_level") or "every_turn").strip().lower()
    if level == "silent":
        return False
    if level == "every_turn":
        return True
    if "report_important" in poll:
        return bool(poll.get("report_important"))
    names = {str(a.get("action")) for a in (poll.get("legal_actions") or []) if isinstance(a, dict)}
    return any(n != "chat" for n in names)


def _restart_pending(payload: dict | None) -> bool:
    prefs = (payload or {}).get("agent_preferences") or {}
    requested_at = str(prefs.get("watcher_restart_requested_at") or "")
    ack_at = str(prefs.get("watcher_restart_ack_at") or "")
    return bool(requested_at and (not ack_at or requested_at > ack_at))


def _ack_and_restart(token: str, schema: dict, payload: dict | None) -> bool:
    """Acknowledge one server-authorized idle restart, then replace this process."""
    if not _restart_pending(payload):
        return False
    status_code, response = arena_client.heartbeat_with_response(
        token,
        schema,
        restart_ack=True,
    )
    if status_code != 200 or _restart_pending(response):
        log(f"restart acknowledgement failed ({status_code}); continuing safely")
        return False
    log("dashboard restart acknowledged; replacing runner process")
    os.execv(
        sys.executable,
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawArena BYO agent runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="poll until it's your turn, print the move that WOULD be "
                             "submitted, then exit — no bonus claim, no heartbeat, no action")
    parser.add_argument("--matches", type=int, default=0,
                        help="stop after N finished matches (0 = run forever)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="verify model, schema, token and heartbeat, then exit before polling")
    args = parser.parse_args()
    if args.matches < 0:
        parser.error("--matches must be 0 or greater")
    # LLM connection is REQUIRED for arena play: ClawArena games are live PvP
    # decided by your model each turn. The built-in heuristic exists only as a
    # per-turn safety fallback, not as a playing mode. (Offline tools —
    # check.py / mock_arena.py — run without a key.)
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("CLAWARENA_GATEWAY_KEY")
            or os.environ.get("CLAWARENA_ALLOW_KEYLESS")):
        print(
            "An LLM connection is required to play.\n"
            "  # Recommended: DeepSeek V4 Flash\n"
            "  export LLM_API_KEY=\"...\"\n"
            "  export LLM_BASE_URL=\"https://api.deepseek.com/v1\"\n"
            "  export LLM_MODEL=\"deepseek-v4-flash\"\n"
            "  # Any OpenAI-compatible provider remains supported.\n"
            "  # or CLAWARENA_GATEWAY_KEY=...        # an issued arena gateway key\n"
            "Coding-agent play needs no provider key: python3 play.py\n"
            "Offline testing needs no key: python3 check.py / python3 mock_arena.py",
            flush=True,
        )
        return 2

    if not os.environ.get("CLAWARENA_SKIP_PREFLIGHT"):
        try:
            model_route = brain.preflight()
        except Exception as exc:  # noqa: BLE001 — startup must fail loudly, not silently downgrade
            log(
                "model preflight failed; the runner was NOT started and will not "
                f"silently play heuristics ({exc})"
            )
            return 2
        log(f"model preflight ok ({model_route})")

    token = arena_client.connection_token()
    schema = arena_client.fetch_schema()

    if args.preflight_only:
        heartbeat_status = arena_client.heartbeat(token, schema)
        if heartbeat_status != 200:
            log(f"preflight heartbeat failed ({heartbeat_status})")
            return 1
        log("preflight complete; no match was polled or joined")
        return 0

    # Best-effort daily bonus. An unclaimed agent receives 409 because placeholder
    # accounts are never funded; the real owner gets the bonus atomically when
    # they claim. On later starts this call is an idempotent top-up attempt.
    # Skipped in --dry-run because a preview must not mutate server state.
    if not args.dry_run:
        try:
            agent_id, auth_token = arena_client.decode_connection_token(token)
            status_code, bonus = arena_client.claim_daily_bonus(agent_id, auth_token)
            if status_code == 200:
                log(f"daily bonus claimed: +{bonus.get('amount')} CP (balance {bonus.get('balance')})")
            else:
                log(f"daily bonus: {bonus.get('detail') or bonus.get('message') or status_code}")
        except Exception as exc:  # noqa: BLE001 — funding is best-effort
            log(f"daily bonus skipped ({exc})")
    heartbeat_every = max(15, int(schema["heartbeat"]["grace_seconds"]) - 30)
    log(f"schema ok (protocol {schema['protocol_version']}); "
        f"heartbeat identity {schema['heartbeat']['identity']['skill_version']}")
    # No literal fee here: it is set per environment and moved once already.
    # GET /api/v1/games/rules/ is the live number.
    log("costs: an entry fee is STAKED from your account each match "
        "(see /api/v1/games/rules/ for the current amount); "
        "winner takes the pot minus a 10% platform fee. LLM inference is on your "
        "key (measured: typically under $0.01/match on flash-tier models). "
        f"{'This runner keeps polling between matches — the server queues NEW matches only while the agent Play Mode is Continuous (the default is One Match, which pauses after the first). Ctrl-C to stop.' if not args.matches else ''}")

    # Identity BEFORE the first poll: polling stamps a heartbeat timestamp on
    # the agent row, and a server safety sweep pauses autoplay for agents that
    # look alive but report no client identity — heartbeat-first closes that
    # window (the pause is sticky and needs a manual re-enable). Skipped in
    # --dry-run (a preview must not stamp identity or keep the agent "alive").
    if not args.dry_run:
        initial_heartbeat, heartbeat_payload = arena_client.heartbeat_with_response(
            token,
            schema,
        )
        if initial_heartbeat != 200:
            log(f"initial heartbeat failed ({initial_heartbeat}) — exiting")
            return 1
        _ack_and_restart(token, schema, heartbeat_payload)
        ready_file = os.environ.get("CLAWARENA_READY_FILE", "").strip()
        if ready_file:
            try:
                Path(ready_file).write_text(str(os.getpid()))
            except OSError as exc:
                log(f"could not signal setup readiness ({exc}) — exiting")
                return 1
    last_heartbeat = time.time()
    last_acted_window_id = None
    finished_matches = 0
    playing_match_id = None
    last_status_message = None
    exit_after_active_match = False
    # The owner's dashboard guidance is delivered ONE-SHOT (consumed server-side
    # on the poll that carries it, even while waiting between matches) and only
    # re-sent when it changes. Latch it match-independently so a hint that lands
    # on a queueing poll isn't lost before the next match starts. message_language
    # is NOT delta-stripped (always present) but latch it the same way.
    standing_prefs = {}
    standing_language = ""
    rejected_actions: set[str] = set()  # actions rejected in the current decision window
    reject_window_id = None
    pending_submission = None  # exact payload to retry after an uncertain transport result
    poll_failures = 0
    action_rejection = None  # one server-authored correction turn for Diplomacy
    diplomacy_corrections = 0
    diplomacy_fallback_attempted = False
    window_fallback_attempted = False
    forced_submission = None
    action_span_started = None
    # The server delivers game_rules_brief and strategy_brief once per match
    # (and replays them on an explicit restart resync). Cache both top-level keys
    # per match and ride them into decide()'s state so the LLM (which
    # serializes state) always sees the authoritative rules + role strategy.
    match_briefs = {}
    # The first successful poll after every local runner start asks the server
    # to replay the full match baseline and one-shot guidance. This makes a
    # mid-match process/container restart self-healing without user setup.
    needs_context_resync = True
    resync_context_id = uuid.uuid4().hex
    # Under the delta transport the board arrives as a diff and this object is
    # what turns it back into the complete board the rest of this loop expects.
    # It is deliberately the ONLY thing that knows the wire was a delta.
    board_state = match_state.MatchState()
    needs_board_bootstrap = True

    while True:
        # Remember what THIS poll asked for: a brain that keeps a session needs
        # to know the board it is holding came back whole, so it can hand the
        # whole thing over instead of diffing it against a copy the runtime may
        # no longer have.
        asked_for_whole_board = needs_context_resync or needs_board_bootstrap
        status_code, poll = arena_client.poll(
            token,
            wait=30,
            resync=needs_context_resync,
            context_id=resync_context_id if needs_context_resync else None,
            state_ack=board_state.ack(),
            bootstrap=needs_board_bootstrap,
        )
        if status_code == 401:
            log("token rejected (rotated?) — exiting")
            return 1
        if status_code != 200:
            poll_failures += 1
            retry_delay = _poll_retry_delay(poll_failures, status_code)
            log(
                f"poll {status_code}: {str(poll)[:120]} — retry "
                f"{poll_failures} in {retry_delay:.2f}s"
            )
            time.sleep(retry_delay)
            continue
        if poll_failures:
            log(f"poll recovered after {poll_failures} failures — retry backoff reset")
            poll_failures = 0
        needs_context_resync = False

        if arena_client.delta_transport_enabled():
            complete = _materialize_board(board_state, poll)
            if complete is _NEEDS_RESYNC:
                # Refuse to act on a board we cannot prove. One extra poll costs
                # a turn's latency; acting on a diverged board costs the match.
                log(f"board resync: {board_state.last_error}")
                needs_board_bootstrap = True
                # Ask for the projection cursor to be rebuilt as well, not just
                # a full board. Without it the only recovery signal is the
                # bootstrap profile, and a server that replays a same-sequence
                # response answers the retry with the very delta we just
                # refused -- for the whole turn, until it plays for us.
                needs_context_resync = True
                continue
            needs_board_bootstrap = False

        # Agent Control restart is guarded server-side to idle/paused/no-match.
        # Poll carries the same timestamps as heartbeat, so a long-polling runner
        # can acknowledge promptly instead of waiting for its next keep-alive.
        _ack_and_restart(token, schema, poll)

        status = poll.get("status", "idle")
        match_id = poll.get("match_id")
        prefs = poll.get("agent_preferences") or {}
        if prefs:
            # Latch the one-shot guidance and language on EVERY poll, not only
            # playing ones — the server may deliver (and consume) them while the
            # agent is still queueing, and dropping them there loses the owner's
            # hint/risk/language for the whole next match.
            if prefs.get("current_strategy_hint") or prefs.get("current_risk_profile"):
                standing_prefs = {
                    "strategy_hint": prefs.get("current_strategy_hint"),
                    "risk_profile": prefs.get("current_risk_profile"),
                }
            elif prefs.get("current_strategy_hint_cleared"):
                standing_prefs = {}
            if prefs.get("message_language"):
                standing_language = prefs["message_language"]

        # A finished match can requeue between polls (autoplay continuous), so a
        # "finished" status is not guaranteed to be observed — also count the
        # transition away from a match we were playing.
        # Count only matches this process actually observed as playing. A fresh
        # --matches 1 runner can initially receive the previous process's stale
        # `finished` projection; counting that would exit before the next match.
        match_over = playing_match_id is not None and (
            status != "playing" or match_id != playing_match_id
        )
        if match_over:
            finished_matches += 1
            last_acted_window_id = None
            pending_submission = None
            finished_id = playing_match_id or match_id
            match_briefs.pop(playing_match_id, None)
            target_reached = bool(args.matches) and finished_matches >= args.matches
            memory.end_match(finished_id)
            log(f"match {finished_id} finished ({finished_matches} total)")
            playing_match_id = match_id if status == "playing" else None
            if target_reached and status == "playing":
                if not exit_after_active_match:
                    log(
                        "match limit reached, but the server already assigned the next match; "
                        "finishing that active match before exit"
                    )
                exit_after_active_match = True
            elif target_reached or exit_after_active_match:
                return 0
            if status == "finished":
                continue

        if status != "playing":
            # Surface the server's own explanation (queue wait, autopause reason,
            # insufficient HP, deployment hold, ...) instead of a silent frozen
            # terminal or incorrectly blaming a lack of opponents.
            message = _queue_status_message(poll)
            if message != last_status_message:
                log(f"[{status}] {message}")
                last_status_message = message
            # Keep-alive while queueing: without the heartbeat (+ identity from
            # the schema) the arena safety-pauses autoplay within grace_seconds.
            # (--dry-run never keeps the agent alive — it only previews a move.)
            if not args.dry_run and time.time() - last_heartbeat > heartbeat_every:
                hb_status, heartbeat_payload = arena_client.heartbeat_with_response(
                    token,
                    schema,
                )
                if hb_status != 200:
                    log(f"heartbeat failed ({hb_status}) — autoplay may get safety-paused")
                else:
                    _ack_and_restart(token, schema, heartbeat_payload)
                last_heartbeat = time.time()
            continue

        playing_match_id = match_id
        # Cache one-shot deliveries BEFORE any turn gate below can `continue`:
        # briefs arrive when the server chooses, and the dashboard guidance
        # (requested via consume_preferences=1) is consumed server-side on the
        # poll that carries it — even an opponent-turn poll — then re-sent only
        # when the value changes.
        briefs = match_briefs.setdefault(match_id, {})
        for brief_key in ("game_rules_brief", "strategy_brief"):
            if poll.get(brief_key):
                briefs[brief_key] = poll[brief_key]
        # Fold the latched owner guidance into this match's briefs. standing_prefs
        # was captured above from whatever poll delivered it (possibly a queueing
        # poll before this match), so it survives the one-shot server delivery.
        if standing_prefs:
            briefs["user_preferences"] = dict(standing_prefs)
        else:
            briefs.pop("user_preferences", None)

        if not poll.get("is_your_turn"):
            continue
        seq = poll.get("seq")
        action_window_id = poll.get("action_window_id") or seq
        if action_window_id is not None and action_window_id == last_acted_window_id:
            # Already acted on this state. The long-poll returns instantly while
            # is_your_turn is still true (until the runner tick consumes our
            # queued action), so sleep or we'd spin into the 60/min rate limit.
            time.sleep(1.5)
            continue
        if action_window_id != reject_window_id:
            # New decision window — forget actions rejected for the old choice.
            # The poll seq may change for unrelated events while this window is
            # stable, so key model retries to the server's shared window id.
            rejected_actions.clear()
            action_rejection = None
            diplomacy_corrections = 0
            diplomacy_fallback_attempted = False
            window_fallback_attempted = False
            forced_submission = None
            reject_window_id = action_window_id
            action_span_started = time.monotonic()
            _action_span(
                action_window_id,
                match_id,
                poll.get("game_type"),
                "received",
                action_span_started,
            )
            if pending_submission and pending_submission["action_window_id"] != action_window_id:
                pending_submission = None
        legal_actions = poll.get("legal_actions") or []
        if not legal_actions:
            time.sleep(1)
            continue
        # Hide actions rejected in THIS decision window so decide() picks
        # a different legal move instead of re-submitting a rejected one in a
        # tight loop (fall back to the full set if every action was rejected).
        usable_actions = [a for a in legal_actions if a.get("action") not in rejected_actions] or legal_actions

        state = dict(poll.get("state") or {})
        state.update(briefs)
        if action_rejection:
            state["action_rejection"] = dict(action_rejection)
        # Some game projections (mafia's poll-refresh path) omit game_type from
        # state while the poll envelope always carries it — inject it so
        # decide()'s per-game dispatch never silently falls through.
        state.setdefault("game_type", poll.get("game_type"))
        # The owner's chat language (agent_preferences.message_language): the LLM
        # prompt honors it for params.message table talk. Absent = English.
        if standing_language:
            state["message_language"] = standing_language
        decision_context = _decision_context_for_turn(
            poll,
            state,
            usable_actions,
            action_rejection,
        )
        if decision_context is not None:
            state["_decision_context"] = decision_context
        # Make this the active match record. It no longer feeds the prompt --
        # the session transcript and its compaction note carry the match now --
        # but it is still what pins session identity and the Hermes resumable
        # session id for the rest of this turn.
        memory.open_match(match_id)
        if asked_for_whole_board:
            state["_full_state_requested"] = True
        decision_budget = _decision_budget(poll)
        state["_decision_budget_seconds"] = decision_budget["effective_seconds"]
        state["_decision_budget_configured_seconds"] = decision_budget[
            "configured_seconds"
        ]
        state["_decision_budget_policy"] = decision_budget["policy"]
        state["_server_turn_remaining_seconds"] = decision_budget[
            "server_remaining_seconds"
        ]
        state["_submit_reserve_seconds"] = decision_budget["submit_reserve_seconds"]
        state["_action_window_id"] = action_window_id
        state["_match_id"] = match_id

        if forced_submission and forced_submission["action_window_id"] == action_window_id:
            move = dict(forced_submission["move"])
            memo = None
            forced_submission = None
            log(f"submitting server-authored fallback: {move['action']}")
        elif pending_submission and pending_submission["action_window_id"] == action_window_id:
            move = dict(pending_submission["move"])
            memo = pending_submission["memo"]
            log(f"retrying unconfirmed action: {move['action']}")
        else:
            _action_span(
                action_window_id,
                match_id,
                state.get("game_type"),
                "inference_started",
                action_span_started or time.monotonic(),
                decision_budget_seconds=round(state["_decision_budget_seconds"], 2),
                configured_budget_seconds=_round_optional_seconds(
                    state["_decision_budget_configured_seconds"],
                ),
                server_remaining_seconds=round(
                    state["_server_turn_remaining_seconds"], 2,
                ),
                submit_reserve_seconds=round(state["_submit_reserve_seconds"], 2),
                decision_budget_policy=state["_decision_budget_policy"],
            )
            if state["_decision_budget_seconds"] < 1.0:
                move = _server_fallback(state, usable_actions)
                if move is not None:
                    log("deadline reserve exhausted — playing server-authored fallback")
                else:
                    log("deadline reserve exhausted — playing deterministic fallback")
                    move = dict(heuristic_agent.decide(state, usable_actions))
            else:
                try:
                    move = dict(brain.decide(state, usable_actions))
                except Exception as exc:  # noqa: BLE001 — a decide bug must not forfeit the match
                    move = _server_fallback(state, usable_actions)
                    if move is not None:
                        log(f"decide() crashed ({exc}) — playing server-authored fallback")
                    else:
                        log(f"decide() crashed ({exc}) — playing first legal action")
                        move = {"action": usable_actions[0].get("action"), "params": {}}
            memo = move.pop("memo", None)
            # The agent's own judgement that it can no longer see part of the
            # board. Popped here so it never reaches the server as an action
            # parameter; it only asks the NEXT poll for a whole board instead of
            # a delta. We do not rate-limit it: what a compaction keeps is the
            # harness's business, and a cap on our side would be us second-
            # guessing the one party that can actually tell.
            if move.pop("need_full_state", False):
                needs_context_resync = True
                log("agent asked for a full board next turn")
            move["idempotency_key"] = helpers.action_idempotency_key(seq, move)
            _action_span(
                action_window_id,
                match_id,
                state.get("game_type"),
                "decision_ready",
                action_span_started or time.monotonic(),
                action=move.get("action"),
            )

        if args.dry_run:
            log(f"[dry-run] would submit: {move}")
            return 0

        pending_submission = {
            "action_window_id": action_window_id,
            "move": dict(move),
            "memo": memo,
        }
        status_code, result = arena_client.act(token, move)
        _action_span(
            action_window_id,
            match_id,
            state.get("game_type"),
            "submitted",
            action_span_started or time.monotonic(),
            action=move.get("action"),
        )
        if (
            _is_transient_action_response(status_code, result)
            and status_code != 429
        ):
            # The server may have queued the action before the ACK was lost.
            # Retry the exact same payload/key once now; if transport is still
            # uncertain, the next poll of this same window retries it again without
            # paying for another model decision.
            log(f"action ACK uncertain ({status_code}) — retrying exact payload")
            time.sleep(1)
            status_code, result = arena_client.act(token, move)
        if status_code == 200:
            pending_submission = None
            last_acted_window_id = action_window_id
            rejected_actions.clear()
            action_rejection = None
            log(f"acted: {move['action']} ({result.get('ack_type', 'ok')})")
            _action_span(
                action_window_id,
                match_id,
                state.get("game_type"),
                "ACKed",
                action_span_started or time.monotonic(),
                action=move.get("action"),
                ack_type=result.get("ack_type", "ok"),
            )
            # Per-turn report on exactly the turns the dashboard report_level +
            # server report_important flag allow (same gate as the OpenClaw
            # watcher). A runtime that can deliver (hermes_agent via Hermes'
            # direct send command) exposes report(); best-effort, never blocks play.
            if _should_deliver(poll) and hasattr(brain, "report"):
                try:
                    # The memo was popped off `move` before submission (the
                    # server does not take it), but it is the one line that says
                    # WHY — restore it for the report only.
                    brain.report(state, {**move, "memo": memo} if memo else move)
                except Exception as exc:  # noqa: BLE001 — a report must not break play
                    log(f"report skipped ({exc})")
            time.sleep(0.5)
        elif status_code == 409 and result.get("code") == "action_already_queued":
            pending_submission = None
            last_acted_window_id = action_window_id  # queued — success-equivalent
            rejected_actions.clear()
            action_rejection = None
            _action_span(
                action_window_id,
                match_id,
                state.get("game_type"),
                "ACKed",
                action_span_started or time.monotonic(),
                action=move.get("action"),
                ack_type="action_already_queued",
            )
            time.sleep(0.5)
        elif status_code == 409 and _is_transient_action_response(status_code, result):
            # A racing engine tick can answer "turn is updating; poll and retry"
            # while keeping the same action_window_id. Preserve the terminal
            # decision and retry its exact idempotent payload; never pay for a
            # second model decision in this window.
            log(
                f"action window updating ({str(result.get('message') or result)[:140]}) "
                "— retrying cached decision"
            )
            time.sleep(3)
        elif status_code == 401:
            log("action token rejected (rotated?) — exiting")
            return 1
        elif _is_transient_action_response(status_code, result):
            log(f"action still unconfirmed {status_code}: {str(result)[:140]} — retrying same payload")
            time.sleep(10 if status_code == 429 else 3)
        else:
            # Diplomacy gets one structured correction and then the current
            # server-authored fallback. Other games keep action-name backoff.
            pending_submission = None
            is_diplomacy = state.get("game_type") == "diplomacy"
            if is_diplomacy and status_code == 400 and diplomacy_corrections < 1:
                diplomacy_corrections += 1
                action_rejection = {
                    "status": status_code,
                    "code": result.get("code") or "invalid_diplomacy_action",
                    "message": result.get("message") or result.get("detail") or "Action rejected",
                    "rejected_action": move.get("action"),
                    "correction_attempt": diplomacy_corrections,
                    **({"field": result.get("field")} if result.get("field") else {}),
                    **(
                        {"invalid_value": result.get("invalid_value")}
                        if "invalid_value" in result
                        else {}
                    ),
                    **(
                        {"allowed_values": result.get("allowed_values")}
                        if isinstance(result.get("allowed_values"), list)
                        else {}
                    ),
                }
                log(
                    f"diplomacy action rejected {status_code}: "
                    f"{action_rejection['message'][:140]} — one corrective model turn"
                )
                time.sleep(1)
            elif is_diplomacy and status_code == 400 and not diplomacy_fallback_attempted:
                fallback = _server_fallback(state, legal_actions)
                if fallback is None:
                    fallback = helpers.diplomacy_server_fallback(
                        move.get("action"),
                        legal_actions,
                    )
                if fallback:
                    fallback["idempotency_key"] = helpers.action_idempotency_key(seq, fallback)
                    forced_submission = {
                        "action_window_id": action_window_id,
                        "move": fallback,
                    }
                    diplomacy_fallback_attempted = True
                    action_rejection = None
                    log(
                        f"diplomacy correction rejected {status_code} — "
                        "using the server-authored fallback without another model call"
                    )
                    time.sleep(1)
                else:
                    last_acted_window_id = action_window_id
                    log(
                        f"diplomacy action rejected {status_code} with no authorized fallback — "
                        "waiting for the server deadline default"
                    )
                    time.sleep(1.5)
            elif is_diplomacy:
                last_acted_window_id = action_window_id
                action_rejection = None
                if diplomacy_fallback_attempted:
                    log(
                        f"server-authored diplomacy fallback rejected {status_code} — "
                        "waiting for the server deadline default"
                    )
                else:
                    log(
                        f"diplomacy action rejected {status_code} as stale or non-correctable — "
                        "waiting for an authoritative server state change"
                    )
                time.sleep(1.5)
            else:
                rejected_actions.add(move["action"])
                _action_span(
                    action_window_id,
                    match_id,
                    state.get("game_type"),
                    "rejected",
                    action_span_started or time.monotonic(),
                    action=move.get("action"),
                    fallback_reason=str(result.get("code") or status_code)[:120],
                )
                if not window_fallback_attempted:
                    window_fallback_attempted = True
                    fallback_actions = [
                        entry for entry in legal_actions
                        if entry.get("action") not in rejected_actions
                    ]
                    try:
                        allowed_fallbacks = fallback_actions or legal_actions
                        fallback = _server_fallback(state, allowed_fallbacks)
                        if fallback is None:
                            fallback = heuristic_agent.decide(
                                state,
                                allowed_fallbacks,
                            )
                    except Exception:  # noqa: BLE001 - server deadline remains the final guard
                        fallback = None
                    if isinstance(fallback, dict) and fallback.get("action"):
                        fallback = dict(fallback)
                        fallback.pop("memo", None)
                        fallback["idempotency_key"] = helpers.action_idempotency_key(
                            seq,
                            fallback,
                        )
                        forced_submission = {
                            "action_window_id": action_window_id,
                            "move": fallback,
                        }
                        log(
                            f"action rejected {status_code}: {str(result)[:100]} — "
                            "using one cached legal fallback without another inference"
                        )
                        time.sleep(1)
                        continue
                last_acted_window_id = action_window_id
                log(
                    f"action rejected {status_code}: {str(result)[:140]} — "
                    "terminal for this window; waiting for the server deadline"
                )
                time.sleep(1.5)


if __name__ == "__main__":
    sys.exit(main())
