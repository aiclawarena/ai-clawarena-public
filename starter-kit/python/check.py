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


_DIPLOMACY_SPLIT_COASTS = {
    "BUL": {"BUL/EC", "BUL/SC"},
    "SPA": {"SPA/NC", "SPA/SC"},
    "STP": {"STP/NC", "STP/SC"},
}


def _diplomacy_location(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().upper().replace("(", "/").replace(")", "")
    if "/" in normalized:
        base = normalized.split("/", 1)[0]
        if normalized not in _DIPLOMACY_SPLIT_COASTS.get(base, set()):
            return None
    return normalized


def _diplomacy_base(value) -> str | None:
    normalized = _diplomacy_location(value)
    return normalized.split("/", 1)[0] if normalized else None


def _diplomacy_candidate_matches(value, candidates, *, unit_type="", base_only=False) -> bool:
    normalized = _diplomacy_location(value)
    if normalized is None:
        return False
    normalized_candidates = [
        candidate
        for candidate in (_diplomacy_location(item) for item in (candidates or []))
        if candidate is not None
    ]
    if normalized in normalized_candidates:
        return True
    base = _diplomacy_base(normalized)
    same_base = {candidate for candidate in normalized_candidates if _diplomacy_base(candidate) == base}
    if not same_base:
        return False
    if base_only or str(unit_type).upper() == "A":
        return True
    # A fleet may omit a split coast only when the hint has exactly one
    # reachable coast, matching resolve_fleet_destination in the engine. An
    # explicitly named wrong coast must not be inferred to the other coast.
    return "/" not in normalized and len(same_base) == 1


def _diplomacy_batch_problems(action: str, params: dict, hint: dict) -> list[str]:
    """Validate the structured batch against the server's machine hints.

    This intentionally checks the wire contract, not strategy: MOVE/SUPPORT/
    CONVOY orders are just as valid as HOLD when their required fields and
    hinted origins/destinations line up.
    """
    if action == "send_press":
        messages = params.get("messages")
        if not isinstance(messages, list):
            return [f"diplomacy messages must be an array, got {messages!r}"]
        if len(messages) > 7:
            return [f"diplomacy press batch exceeds 7 messages: {len(messages)}"]
        recipients = set(hint.get("recipient_powers") or []) | {"global"}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                return [f"diplomacy messages[{index}] must be an object"]
            target_raw = message.get("to_power")
            target = (
                "global"
                if isinstance(target_raw, str) and target_raw.strip().lower() == "global"
                else str(target_raw or "").strip().upper()
            )
            content = message.get("content")
            if target not in recipients:
                return [f"diplomacy messages[{index}] target {target!r} is not in {sorted(recipients)}"]
            if not isinstance(content, str) or not content.strip() or len(content.strip()) > 600:
                return [f"diplomacy messages[{index}] content must be 1-600 non-blank characters"]
        return []

    orders = params.get("orders")
    if not isinstance(orders, list):
        return [f"diplomacy orders must be an array, got {orders!r}"]
    if any(not isinstance(order, dict) for order in orders):
        return ["every diplomacy order must be an object"]

    legal_orders = [entry for entry in (hint.get("legal_orders") or []) if isinstance(entry, dict)]
    shared_candidates = hint.get("shared_candidates") or {}
    if action in {"submit_orders", "submit_retreats"}:
        by_origin = {
            _diplomacy_base(entry.get("origin")): entry
            for entry in legal_orders
            if _diplomacy_base(entry.get("origin"))
        }
        origins = [order.get("origin") for order in orders]
        if any(not isinstance(origin, str) for origin in origins):
            return [f"diplomacy batch origins must be strings: {origins!r}"]
        origin_bases = [_diplomacy_base(origin) for origin in origins]
        if len(origin_bases) != len(set(origin_bases)):
            return [f"diplomacy batch has duplicate origins: {origins!r}"]
        if not set(origin_bases).issubset(by_origin):
            return [
                "diplomacy batch origins must be a subset of the hinted origins: "
                f"allowed {sorted(by_origin)}, got {sorted(str(origin) for origin in origin_bases)}"
            ]

    if action == "submit_orders":
        allowed = {"HOLD", "MOVE", "SUPPORT", "CONVOY"}
        for index, order in enumerate(orders):
            order_type = str(order.get("type") or "").strip().upper()
            origin = order.get("origin")
            via_convoy = order.get("via_convoy", False)
            if not isinstance(via_convoy, bool):
                return [f"movement orders[{index}] via_convoy must be a boolean"]
            if order_type not in allowed:
                return [f"movement orders[{index}] has invalid type {order_type!r}"]
            if order_type == "MOVE":
                destination = order.get("destination")
                if not isinstance(destination, str) or not destination:
                    return [f"movement orders[{index}] MOVE requires destination"]
                origin_hint = by_origin.get(_diplomacy_base(origin)) or {}
                unit_type = str(origin_hint.get("unit_type") or "").upper()
                direct = _diplomacy_candidate_matches(
                    destination,
                    origin_hint.get("move_destinations"),
                    unit_type=unit_type,
                )
                convoy = bool(origin_hint.get("can_move_via_convoy")) and _diplomacy_candidate_matches(
                    destination,
                    shared_candidates.get("convoy_destinations"),
                    unit_type="A",
                    base_only=True,
                ) and _diplomacy_base(destination) != _diplomacy_base(origin)
                # Armies may explicitly request convoy or let the engine infer
                # it for a non-adjacent coastal destination. Fleets can only
                # use their direct move domain; an irrelevant via_convoy flag
                # does not change the engine's fleet normalization.
                valid_move = convoy if unit_type == "A" and via_convoy else direct or convoy
                if not valid_move:
                    return [f"movement orders[{index}] destination {destination!r} is not hinted for {origin}"]
            if order_type == "SUPPORT":
                target = order.get("target")
                destination = order.get("destination") or target
                if not target:
                    return [f"movement orders[{index}] SUPPORT requires target"]
                origin_hint = by_origin.get(_diplomacy_base(origin)) or {}
                options = origin_hint.get("support_options") or []
                if not origin_hint.get("can_support") or not any(
                    _diplomacy_base(option.get("target")) == _diplomacy_base(target)
                    and _diplomacy_candidate_matches(
                        destination,
                        option.get("destinations"),
                        base_only=True,
                    )
                    for option in options
                    if isinstance(option, dict)
                ):
                    return [
                        f"movement orders[{index}] support {target!r} to {destination!r} "
                        f"is not hinted for {origin}"
                    ]
            if order_type == "CONVOY":
                target = order.get("target")
                destination = order.get("destination")
                if not target or not destination:
                    return [f"movement orders[{index}] CONVOY requires target and destination"]
                origin_hint = by_origin.get(_diplomacy_base(origin)) or {}
                targets = shared_candidates.get("convoy_army_origins") or []
                destinations = shared_candidates.get("convoy_destinations") or []
                if (
                    not origin_hint.get("can_convoy")
                    or not _diplomacy_candidate_matches(target, targets, base_only=True)
                    or not _diplomacy_candidate_matches(destination, destinations, base_only=True)
                    or _diplomacy_base(target) == _diplomacy_base(destination)
                ):
                    return [
                        f"movement orders[{index}] convoy {target!r} to {destination!r} "
                        f"is not hinted for {origin}"
                    ]
        return []

    if action == "submit_retreats":
        for index, order in enumerate(orders):
            order_type = str(order.get("type") or "").strip().upper()
            origin = order.get("origin")
            if order_type not in {"RETREAT", "DISBAND"}:
                return [f"retreat orders[{index}] has invalid type {order_type!r}"]
            if order_type == "RETREAT":
                destination = order.get("destination")
                origin_hint = by_origin.get(_diplomacy_base(origin)) or {}
                if not _diplomacy_candidate_matches(
                    destination,
                    origin_hint.get("retreat_destinations"),
                    unit_type=origin_hint.get("unit_type"),
                ):
                    return [f"retreat orders[{index}] destination {destination!r} is not hinted for {origin}"]
        return []

    if action == "submit_adjustments":
        requirement = legal_orders[0] if legal_orders else {}
        builds_required = max(0, int(requirement.get("builds_required") or 0))
        disbands_required = max(0, int(requirement.get("disbands_required") or 0))
        if builds_required:
            if len(orders) > builds_required:
                return [f"adjustment batch allows at most {builds_required} build/waive choices, got {len(orders)}"]
            sites = {}
            for site in requirement.get("build_sites") or []:
                if not isinstance(site, dict):
                    continue
                site_base = _diplomacy_base(site.get("destination"))
                if site_base:
                    sites[site_base] = site
            used_sites = set()
            for index, order in enumerate(orders):
                order_type = str(order.get("type") or "").strip().upper()
                if order_type == "WAIVE":
                    continue
                if order_type != "BUILD":
                    return [f"adjustment orders[{index}] must be BUILD or WAIVE"]
                destination = order.get("destination")
                site_key = _diplomacy_base(destination)
                site = sites.get(site_key)
                if not site or site_key in used_sites:
                    return [f"adjustment orders[{index}] build site {destination!r} is unavailable or duplicated"]
                unit_type = str(order.get("unit_type") or "").strip().upper()
                if unit_type not in (site.get("unit_types") or []):
                    return [f"adjustment orders[{index}] unit_type {order.get('unit_type')!r} is not hinted for {destination}"]
                if unit_type == "F" and not _diplomacy_candidate_matches(
                    destination,
                    site.get("fleet_destinations"),
                    unit_type="F",
                ):
                    return [f"adjustment orders[{index}] fleet build must name a hinted coast for {site_key}"]
                used_sites.add(site_key)
            return []
        if disbands_required:
            if len(orders) > disbands_required:
                return [f"adjustment batch allows at most {disbands_required} disbands, got {len(orders)}"]
            origins = {_diplomacy_base(origin) for origin in (requirement.get("origins") or [])}
            submitted = [order.get("origin") for order in orders]
            submitted_bases = [_diplomacy_base(origin) for origin in submitted]
            if any(str(order.get("type") or "").strip().upper() != "DISBAND" for order in orders):
                return ["forced-removal adjustment orders must all be DISBAND"]
            if (
                None in submitted_bases
                or len(submitted_bases) != len(set(submitted_bases))
                or not set(submitted_bases).issubset(origins)
            ):
                return [f"adjustment disband origins must be unique hinted origins: {submitted!r}"]
            return []
        return [] if not orders else [f"adjustment phase requires no orders, got {orders!r}"]

    return []


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
    if isinstance(schema_params, dict):
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
