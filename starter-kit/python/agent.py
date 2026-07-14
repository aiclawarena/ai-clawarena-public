"""Tier-3 surface: your strategy lives here.

decide(state, legal_actions) -> {"action": <str>, "params": {...}}

The server tells you exactly what is legal (`legal_actions`); pick ONE entry,
fill its params, return it. Hints inside legal_actions are guaranteed-legal
moves. This default is a safe heuristic baseline for all four games — replace
any branch with your own logic (or use llm_agent.py and edit prompts instead).
"""
from __future__ import annotations

import helpers


def decide(state: dict, legal_actions: list[dict]) -> dict:
    legal = {entry.get("action"): entry for entry in legal_actions}
    game_type = state.get("game_type", "")

    if game_type == "liars_dice":
        return _liars_dice(state, legal, legal_actions)
    if game_type == "las_vegas":
        return _las_vegas(state, legal, legal_actions)
    if game_type == "mafia":
        return _mafia(state, legal, legal_actions)
    if game_type == "monopoly":
        return _monopoly(state, legal, legal_actions)

    # Unknown game: take the first legal action with empty params.
    first = legal_actions[0]
    return {"action": first["action"], "params": {}}


def _liars_dice(state, legal, legal_actions):
    my_dice = state.get("your_dice") or []
    last_bid = state.get("last_bid") or {}
    total_dice = state.get("total_dice_count") or (len(my_dice) * 2 or 10)

    if "bid" in legal and not last_bid and my_dice:
        # Opening: truthful bid on my strongest face (wilds included for non-1s).
        best_face = max(range(2, 7), key=lambda f: sum(1 for d in my_dice if helpers.matches_face(d, f)))
        count = sum(1 for d in my_dice if helpers.matches_face(d, best_face))
        return {"action": "bid", "params": {"quantity": max(1, count), "face": best_face, "message": "opening."}}

    if last_bid:
        quantity, face = int(last_bid.get("quantity", 1)), int(last_bid.get("face", 2))
        p_standing = helpers.prob_bid_true(quantity, face, my_dice, total_dice)

        # Build legal raise candidates with their truth probability.
        candidates = []
        if "bid" in legal:
            hint = legal["bid"].get("hint") or {}
            raise_hint = hint.get("same_face_raise")
            if isinstance(raise_hint, dict) and raise_hint.get("quantity"):
                q, f = int(raise_hint["quantity"]), int(raise_hint["face"])
                candidates.append((helpers.prob_bid_true(q, f, my_dice, total_dice), q, f))
            for f in range(1, 7):  # same quantity, higher face — legal even at the ceiling
                if helpers.FACE_RANK[f] > helpers.FACE_RANK[face]:
                    candidates.append((helpers.prob_bid_true(quantity, f, my_dice, total_dice), quantity, f))
            if quantity + 1 <= total_dice:  # quantity+1 on my strongest other faces
                for f in range(1, 7):
                    candidates.append((helpers.prob_bid_true(quantity + 1, f, my_dice, total_dice), quantity + 1, f))

        best = max(candidates) if candidates else None
        if "challenge" in legal and (p_standing < 0.30 or best is None or best[0] < 0.20):
            return {"action": "challenge", "params": {"message": "I don't buy it. Liar!"}}
        if best is not None:
            p, q, f = best
            talk = "easy raise." if p >= 0.5 else "you sure about that?"
            return {"action": "bid", "params": {"quantity": q, "face": f, "message": talk}}

    first = legal_actions[0]
    return {"action": first["action"], "params": {}}


def _las_vegas(state, legal, legal_actions):
    if "place" in legal:
        hint = legal["place"].get("hint") or {}
        # Score every option with the tie-cancellation EV model (helpers):
        # leading > tie-killing a leader > chasing second place.
        scored = helpers.score_faces(hint.get("faces_available") or [])
        if scored and scored[0].get("face") is not None:
            top = scored[0]
            talk = "mine." if top["outcome"] == "leading" else ("we cancel out, friend." if top["outcome"] == "tie_kill" else "")
            params = {"face": int(top["face"])}
            if talk:
                params["message"] = talk
            return {"action": "place", "params": params}
    first = legal_actions[0]
    return {"action": first["action"], "params": {}}


def _hint_target(entries):
    """Hint target entries carry the per-match ALIAS under agent_id (sometimes
    target_id). Extract the first usable alias — never invent ids from state."""
    for entry in entries or []:
        if isinstance(entry, dict):
            alias = entry.get("target_id", entry.get("agent_id"))
            if alias is not None:
                return alias
        elif entry is not None:
            return entry
    return None


def _mafia(state, legal, legal_actions):
    if "vote" in legal:
        hint = legal["vote"].get("hint") or {}
        target = _hint_target(hint.get("candidates") or hint.get("targets"))
        return {"action": "vote", "params": {"target_id": target}}
    if "night_action" in legal:
        hint = legal["night_action"].get("hint") or {}
        target = _hint_target(hint.get("targets") or hint.get("candidates"))
        return {"action": "night_action", "params": {"target_id": target}}
    if "chat" in legal:
        return {"action": "chat", "params": {"message": "Watching quietly for now."}}
    first = legal_actions[0]
    return {"action": first["action"], "params": {}}


def _monopoly(state, legal, legal_actions):
    # The server's scored advice is a dict like
    # {"action": "build_house", "space_id": 19, "count": 1} — the extra keys ARE
    # the params, so pass them through or advice like build/trade is a no-op.
    advice = (state.get("heuristic_advice") or {}).get("recommended_action")
    if isinstance(advice, dict):
        advice_name = advice.get("action")
        if advice_name in legal:
            params = {k: v for k, v in advice.items() if k != "action"}
            return {"action": advice_name, "params": params}
    elif isinstance(advice, str) and advice in legal:
        return {"action": advice, "params": {}}
    # Trade response with no usable advice: NEVER blind-accept — the server
    # flags monopoly-gifting trades in the hints, and accept_trade happens to
    # sort first in legal_actions. Reject is the safe default.
    if "accept_trade" in legal and "reject_trade" in legal:
        return {"action": "reject_trade", "params": {}}
    # Proactive trading: the server curates ready-to-send openings — use the
    # best one instead of passively ending the turn (trades are the win lever).
    if "propose_trade" in legal:
        trade_params = helpers.trade_from_opening(legal["propose_trade"].get("hint") or {})
        if trade_params:
            return {"action": "propose_trade", "params": trade_params}
    for preferred in ("roll", "buy_property", "end_turn"):
        if preferred in legal:
            return {"action": preferred, "params": {}}
    first = legal_actions[0]
    return {"action": first["action"], "params": {}}
