"""The 50ms offline loop: run your decide() against frozen real-wire fixtures.

    python3 check.py            # all fixtures, heuristic path
    python3 check.py --llm      # include your LLM (needs LLM_API_KEY; costs a few calls)

Fixtures in fixtures/*.json are generated from the LIVE engines + the same
projection code that builds real poll responses (see _fixture.note in each),
so passing here means your bot returns a legal, sane action for that exact
wire shape — this is where a bug like "crashes on the first advised monopoly
turn" dies before it ever costs you a staked match.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# The diplomacy hint-contract validator is shared with the LIVE decision path
# (llm_agent.py validates every batch against the same rules before submit).
from helpers import diplomacy_batch_problems as _diplomacy_batch_problems

# reflection_context.json is a post-match wire shape, not a turn — checked separately.
FIXTURES = sorted(p for p in Path(__file__).parent.glob("fixtures/*.json")
                  if not p.stem.startswith("reflection"))
REFLECTION_FIXTURE = Path(__file__).parent / "fixtures" / "reflection_context.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def runner_state(fixture: dict) -> dict:
    """Mirror runner.py's brief injection so decide() sees what it sees live."""
    state = dict(fixture.get("state") or {})
    for key in ("game_rules_brief", "strategy_brief"):
        if fixture.get(key):
            state[key] = fixture[key]
    state.setdefault("game_type", fixture.get("game_type"))
    return state


def check_move(fixture: dict, move: dict) -> list[str]:
    problems = []
    legal = {entry.get("action"): entry for entry in fixture["legal_actions"]}
    name = fixture["_fixture"]["name"]

    if not isinstance(move, dict) or move.get("action") not in legal:
        return [f"action {move.get('action') if isinstance(move, dict) else move!r} not in legal set {sorted(legal)}"]
    params = move.get("params")
    if not isinstance(params, dict):
        problems.append("params is not a dict")
        params = {}

    # Generic contract: every key the action's params SCHEMA declares must be
    # present, EXCEPT `message` (table talk is always optional). A legal action
    # name with missing/garbage params is exactly what the server rejects (and
    # the kit used to re-submit in a loop) — catch it here. "int or null" keys
    # accept an explicit null, so presence (not truthiness) is the test.
    schema_params = legal.get(move["action"], {}).get("params")
    if isinstance(schema_params, dict) and fixture.get("game_type") != "diplomacy":
        missing = [k for k in schema_params if k != "message" and k not in params]
        if missing:
            problems.append(f"action {move['action']} missing required params {missing} "
                            f"(schema declares {sorted(schema_params)})")

    # liars_dice bid: quantity must be a positive int and face a die face —
    # a string/zero/None quantity is a silent illegal-bid reject on the wire.
    if fixture.get("game_type") == "liars_dice" and move["action"] == "bid":
        q, f = params.get("quantity"), params.get("face")
        if not isinstance(q, int) or isinstance(q, bool) or q < 1:
            problems.append(f"bid quantity must be a positive int, got {q!r}")
        if not isinstance(f, int) or isinstance(f, bool):
            problems.append(f"bid face must be an int, got {f!r}")

    # Spot-specific invariants (the exact bug classes we have been bitten by):
    if name == "liars_ceiling":
        hint = legal.get("bid", {}).get("hint") or {}
        if "same_face_raise" in hint:
            problems.append("fixture invariant broken: ceiling spot offers same_face_raise")
    if name == "monopoly_turn":
        advice = (fixture["state"].get("heuristic_advice") or {}).get("recommended_action")
        if isinstance(advice, dict) and advice.get("action") == move["action"]:
            expected = {k: v for k, v in advice.items() if k != "action"}
            if expected and move["params"] != expected:
                problems.append(f"advice params dropped: expected {expected}, got {move['params']}")
    if name == "mafia_vote" and move["action"] == "vote":
        hint = legal.get("vote", {}).get("hint") or {}
        entries = hint.get("candidates") or hint.get("targets") or []
        valid = {e.get("target_id", e.get("agent_id")) if isinstance(e, dict) else e for e in entries}
        if valid and move["params"].get("target_id") not in valid:
            problems.append(f"vote target {move['params'].get('target_id')!r} not from hint candidates {sorted(map(str, valid))}")
    if name == "mafia_night_action" and move["action"] == "night_action":
        hint = legal.get("night_action", {}).get("hint") or {}
        entries = hint.get("targets") or hint.get("candidates") or []
        valid = {e.get("agent_id", e.get("target_id")) if isinstance(e, dict) else e for e in entries}
        if valid and move["params"].get("target_id") not in valid:
            problems.append(f"night_action target {move['params'].get('target_id')!r} not from hint targets {sorted(map(str, valid))}")
    if name == "vegas_place" and move["action"] == "place":
        hint = legal.get("place", {}).get("hint") or {}
        entries = hint.get("faces_available") or []
        faces = {e.get("face") if isinstance(e, dict) else e for e in entries}
        if faces and move["params"].get("face") not in faces:
            problems.append(f"placed face {move['params'].get('face')} not in available faces {sorted(map(str, faces))}")
    if fixture.get("game_type") == "diplomacy":
        hint = legal.get(move["action"], {}).get("hint") or {}
        problems.extend(_diplomacy_batch_problems(move["action"], params, hint))
    return problems


def check_reflection(use_llm: bool) -> list[str]:
    """Validate the post-match reflection pipeline against the frozen context.

    Offline: the pure pieces (message build, reply parse, save-payload shaping —
    trim, base guard, unchanged-skip). With --llm: one real reflection call too.
    """
    import reflect

    if not REFLECTION_FIXTURE.exists():
        return ["fixtures/reflection_context.json missing"]
    context = load(REFLECTION_FIXTURE)
    problems = []
    limit = int(context["limits"]["strategy_prompt_max_chars"])

    messages = reflect.build_messages(context, {"my_role": None, "my_moves": [], "my_memos": []})
    if not (len(messages) == 2 and "strategy_prompt" in messages[0]["content"]):
        problems.append("build_messages shape broken")

    parsed = reflect.extract_reflection(
        'noise {"strategy_prompt": "Bid faces you hold.", "reason": "test"} noise')
    if not parsed or parsed["strategy_prompt"] != "Bid faces you hold.":
        problems.append("extract_reflection failed on a valid reply")
    if reflect.extract_reflection("no json here") is not None:
        problems.append("extract_reflection accepted garbage")

    payload = reflect.build_save_payload(context, "X" * (limit + 500), "trim test")
    if payload is None or len(payload["strategy_prompt"]) > limit:
        problems.append(f"oversized prompt not trimmed to {limit}")
    elif payload["base_strategy_prompt"] != (context.get("current_strategy_prompt") or ""):
        problems.append("base_strategy_prompt is not the exact fetched prompt (would 409)")
    elif payload["game_type"] != context["match"]["game_type"] or payload["match_id"] != context["match"]["id"]:
        problems.append("save payload match/game_type mismatch")
    if reflect.build_save_payload(context, "   ", "") is not None:
        problems.append("empty prompt should skip the save (server 400)")

    # The fixture's current prompt may be empty, which would make the
    # unchanged-skip and base-guard checks degenerate — exercise them against
    # a synthetic context that HAS standing coaching.
    seeded = dict(context)
    seeded["current_strategy_prompt"] = "Open on your strongest face. Challenge past 55% bluff odds."
    if reflect.build_save_payload(seeded, seeded["current_strategy_prompt"], "") is not None:
        problems.append("unchanged prompt should skip the save (revision noise)")
    if reflect.build_save_payload(seeded, "  " + seeded["current_strategy_prompt"] + "  ", "") is not None:
        problems.append("whitespace-only rewrite should skip the save")
    changed = reflect.build_save_payload(seeded, "New rule: fold under pressure.", "lesson")
    if changed is None or changed["base_strategy_prompt"] != seeded["current_strategy_prompt"]:
        problems.append("changed prompt must carry the EXACT fetched prompt as base (else 409)")

    if use_llm and not problems:
        base, key, model = __import__("llm_agent")._llm_config()
        if base:
            reply = reflect._chat(base, key, model, messages)
            live = reflect.extract_reflection(reply)
            if not live:
                problems.append(f"live LLM reflection reply unusable: {reply[:120]!r}")
            elif reflect.build_save_payload(context, live["strategy_prompt"], live["reason"]) is None \
                    and live["strategy_prompt"].strip() != (context.get("current_strategy_prompt") or "").strip():
                problems.append("live reflection produced an unsendable payload")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="also run llm_agent.decide (uses your key)")
    args = parser.parse_args()

    if not FIXTURES:
        print("no fixtures found next to check.py")
        return 2

    import agent as heuristic_agent
    brains = [("heuristic", heuristic_agent.decide)]
    if args.llm:
        import llm_agent
        brains.append(("llm", llm_agent.decide))

    failures = 0
    for path in FIXTURES:
        fixture = load(path)
        state = runner_state(fixture)
        for brain_name, decide in brains:
            started = time.perf_counter()
            try:
                move = decide(state, fixture["legal_actions"])
                problems = check_move(fixture, move)
            except Exception as exc:  # noqa: BLE001 — a crash IS the finding
                problems, move = [f"decide() raised {type(exc).__name__}: {exc}"], None
            elapsed_ms = (time.perf_counter() - started) * 1000
            label = f"{fixture['_fixture']['name']:<16} [{brain_name}]"
            if problems:
                failures += 1
                print(f"FAIL {label} {problems[0]}")
            else:
                print(f"ok   {label} -> {move['action']:<14} ({elapsed_ms:.0f}ms)")

    reflection_problems = check_reflection(args.llm)
    if reflection_problems:
        failures += 1
        print(f"FAIL {'reflection':<16} {reflection_problems[0]}")
    else:
        print(f"ok   {'reflection':<16} -> save payload contract holds")

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'} — {len(FIXTURES)} fixtures + reflection")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
