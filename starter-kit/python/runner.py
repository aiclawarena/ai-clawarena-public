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
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

import arena_client
import helpers
import memory
import reflect

# Tier-2 by default: llm_agent uses your LLM key when present and always
# falls back to the agent.py heuristic — safe with or without a key.
# CLAWARENA_BRAIN=hermes routes each decision through Hermes instead, keyless.
# Gameplay uses one resumable match session plus persistent compact match
# memory, and post-match self-learning reflects via Hermes too.
if os.environ.get("CLAWARENA_BRAIN", "").strip().lower() == "hermes":
    os.environ.setdefault("CLAWARENA_ALLOW_KEYLESS", "1")
    import hermes_agent as brain
else:
    import llm_agent as brain


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


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


class ReflectionWorker:
    """Run slow post-match learning without pausing polls or live turns."""

    def __init__(self, token: str):
        self.token = token
        self.jobs: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.closed = False

    def submit(self, match_id, prefs: dict | None = None) -> None:
        if not match_id or self.closed or os.environ.get("CLAWARENA_NO_REFLECT"):
            return
        if self.thread is None:
            self.thread = threading.Thread(
                target=self._run,
                name="clawarena-reflection",
                daemon=True,
            )
            self.thread.start()
        self.jobs.put((match_id, dict(prefs or {})))

    def close(self, *, wait: bool = False, timeout: float = 135) -> None:
        if self.closed:
            return
        self.closed = True
        if self.thread is None:
            return
        self.jobs.put(None)
        if wait:
            self.thread.join(timeout=max(0, timeout))

    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                match_id, prefs = job
                reflect.maybe_reflect(self.token, match_id, prefs=prefs)
            finally:
                self.jobs.task_done()


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawArena BYO agent runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="poll until it's your turn, print the move that WOULD be "
                             "submitted, then exit — no bonus claim, no heartbeat, no action")
    parser.add_argument("--matches", type=int, default=0,
                        help="stop after N finished matches (0 = run forever)")
    parser.add_argument("--no-reflect", action="store_true",
                        help="skip post-match self-learning (one LLM call per finished match)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="verify model, schema, token and heartbeat, then exit before polling")
    args = parser.parse_args()
    if args.matches < 0:
        parser.error("--matches must be 0 or greater")
    if args.no_reflect or args.dry_run:
        # dry-run must be side-effect free: reflection is a real LLM call plus
        # a real server-side Strategy Prompt write.
        os.environ["CLAWARENA_NO_REFLECT"] = "1"

    # LLM connection is REQUIRED for arena play: ClawArena games are live PvP
    # decided by your model each turn. The built-in heuristic exists only as a
    # per-turn safety fallback, not as a playing mode. (Offline tools —
    # check.py / mock_arena.py — run without a key.)
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("CLAWARENA_GATEWAY_KEY")
            or os.environ.get("CLAWARENA_ALLOW_KEYLESS")):
        print(
            "An LLM connection is required to play.\n"
            "  export LLM_API_KEY=\"sk-...\"          # OpenAI by default\n"
            "  # other compatible provider: also set LLM_BASE_URL and LLM_MODEL\n"
            "  # or CLAWARENA_GATEWAY_KEY=...        # an issued arena gateway key\n"
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
                log(f"daily bonus claimed: +{bonus.get('amount')} HP (balance {bonus.get('balance')})")
            else:
                log(f"daily bonus: {bonus.get('detail') or bonus.get('message') or status_code}")
        except Exception as exc:  # noqa: BLE001 — funding is best-effort
            log(f"daily bonus skipped ({exc})")
    heartbeat_every = max(15, int(schema["heartbeat"]["grace_seconds"]) - 30)
    log(f"schema ok (protocol {schema['protocol_version']}); "
        f"heartbeat identity {schema['heartbeat']['identity']['skill_version']}")
    log("costs: entry fees (typically 10 HP/match) are STAKED from your account; "
        "winner takes the pot minus a 10% platform fee. LLM inference is on your "
        "key (measured: typically under $0.01/match on flash-tier models), plus one "
        "post-match self-learning call per match (--no-reflect to skip). "
        f"{'This runner keeps polling between matches — the server queues NEW matches only while the agent Play Mode is Continuous (the default is One Match, which pauses after the first). Ctrl-C to stop.' if not args.matches else ''}")

    # Identity BEFORE the first poll: polling stamps a heartbeat timestamp on
    # the agent row, and a server safety sweep pauses autoplay for agents that
    # look alive but report no client identity — heartbeat-first closes that
    # window (the pause is sticky and needs a manual re-enable). Skipped in
    # --dry-run (a preview must not stamp identity or keep the agent "alive").
    if not args.dry_run:
        initial_heartbeat = arena_client.heartbeat(token, schema)
        if initial_heartbeat != 200:
            log(f"initial heartbeat failed ({initial_heartbeat}) — exiting")
            return 1
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
    last_prefs = {}  # newest agent_preferences seen (reflection reads the toggle)
    reflection_worker = ReflectionWorker(token)
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

    while True:
        status_code, poll = arena_client.poll(
            token,
            wait=30,
            resync=needs_context_resync,
            context_id=resync_context_id if needs_context_resync else None,
        )
        if status_code == 401:
            log("token rejected (rotated?) — exiting")
            return 1
        if status_code != 200:
            log(f"poll {status_code}: {str(poll)[:120]} — backing off")
            time.sleep(10 if status_code == 429 else 3)
            continue
        needs_context_resync = False

        status = poll.get("status", "idle")
        match_id = poll.get("match_id")
        prefs = poll.get("agent_preferences") or {}
        if prefs:
            last_prefs = prefs
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
            reflection_worker.submit(finished_id, prefs=last_prefs)
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
                reflection_worker.close(wait=True)
                return 0
            if status == "finished":
                continue

        if status != "playing":
            # Surface the server's own explanation (queue wait, autopause reason,
            # insufficient HP, ...) instead of a silent frozen terminal.
            message = poll.get("message") or status
            if message != last_status_message:
                log(f"[{status}] {message}")
                last_status_message = message
            # Keep-alive while queueing: without the heartbeat (+ identity from
            # the schema) the arena safety-pauses autoplay within grace_seconds.
            # (--dry-run never keeps the agent alive — it only previews a move.)
            if not args.dry_run and time.time() - last_heartbeat > heartbeat_every:
                hb_status = arena_client.heartbeat(token, schema)
                if hb_status != 200:
                    log(f"heartbeat failed ({hb_status}) — autoplay may get safety-paused")
                last_heartbeat = time.time()
            continue

        playing_match_id = match_id
        # Cache one-shot deliveries BEFORE any turn gate below can `continue`:
        # briefs arrive when the server chooses, and the dashboard guidance
        # (requested via consume_preferences=1) is consumed server-side on the
        # poll that carries it — even an opponent-turn poll — then re-sent only
        # when the value changes (or a post-match reflection saves a new prompt).
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
            reject_window_id = action_window_id
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
        # Some game projections (mafia's poll-refresh path) omit game_type from
        # state while the poll envelope always carries it — inject it so
        # decide()'s per-game dispatch never silently falls through.
        state.setdefault("game_type", poll.get("game_type"))
        # The owner's chat language (agent_preferences.message_language): the LLM
        # prompt honors it for params.message table talk. Absent = English.
        if standing_language:
            state["message_language"] = standing_language
        # Per-match memory: our answer to OpenClaw's accumulating session —
        # explicit, restart-proof, never compacted away.
        state["my_memory"] = memory.begin_turn(match_id, state)

        if pending_submission and pending_submission["action_window_id"] == action_window_id:
            move = dict(pending_submission["move"])
            memo = pending_submission["memo"]
            log(f"retrying unconfirmed action: {move['action']}")
        else:
            try:
                move = dict(brain.decide(state, usable_actions))
            except Exception as exc:  # noqa: BLE001 — a decide bug must not forfeit the match
                log(f"decide() crashed ({exc}) — playing first legal action")
                move = {"action": usable_actions[0].get("action"), "params": {}}
            memo = move.pop("memo", None)
            move["idempotency_key"] = helpers.action_idempotency_key(seq, move)

        if args.dry_run:
            log(f"[dry-run] would submit: {move}")
            return 0

        pending_submission = {
            "action_window_id": action_window_id,
            "move": dict(move),
            "memo": memo,
        }
        status_code, result = arena_client.act(token, move)
        if status_code == 0 or status_code >= 500:
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
            memory.record_move(match_id, move, phase=state.get("phase"))
            memory.record_memo(match_id, memo)
            log(f"acted: {move['action']} ({result.get('ack_type', 'ok')})")
            # Per-turn report on exactly the turns the dashboard report_level +
            # server report_important flag allow (same gate as the OpenClaw
            # watcher). A runtime that can deliver (hermes_agent via Hermes'
            # send_message) exposes report(); best-effort, never blocks play.
            if _should_deliver(poll) and hasattr(brain, "report"):
                try:
                    brain.report(state, move)
                except Exception as exc:  # noqa: BLE001 — a report must not break play
                    log(f"report skipped ({exc})")
            time.sleep(0.5)
        elif status_code == 409 and result.get("code") == "action_already_queued":
            pending_submission = None
            last_acted_window_id = action_window_id  # queued — success-equivalent
            rejected_actions.clear()
            memory.record_move(match_id, move, phase=state.get("phase"))
            memory.record_memo(match_id, memo)
            time.sleep(0.5)
        elif status_code == 401:
            log("action token rejected (rotated?) — exiting")
            return 1
        elif status_code == 0 or status_code == 429 or status_code >= 500:
            log(f"action still unconfirmed {status_code}: {str(result)[:140]} — retrying same payload")
            time.sleep(10 if status_code == 429 else 3)
        else:
            # Rejected (illegal params, act-side 429, transient error). Do NOT
            # hammer: the long-poll returns instantly on our turn, so without a
            # backoff we'd re-decide (a paid LLM call) and re-submit the same
            # move every ~0.5s until the 90s turn timeout. Remember it so the
            # next decide tries a DIFFERENT legal action, and back off.
            pending_submission = None
            rejected_actions.add(move["action"])
            log(f"action rejected {status_code}: {str(result)[:140]} — backing off")
            time.sleep(10 if status_code == 429 else 3)


if __name__ == "__main__":
    sys.exit(main())
