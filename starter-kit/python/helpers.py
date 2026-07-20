"""Per-game math the LLM is bad at — computed here, fed to both the heuristic
and (as numbers in the prompt) your model. Pure stdlib, all deterministic.

    liars_dice : prob_bid_true()      wilds-aware binomial over unknown dice
    las_vegas  : score_faces()        EV + tie-cancellation scoring per face
    monopoly   : trade_from_opening() server trade opening -> legal params
"""
from __future__ import annotations

import hashlib
import json
from math import comb

# Face order used by the arena: 2 < 3 < 4 < 5 < 6 < 1 (1 is highest).
FACE_RANK = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 1: 5}


def action_idempotency_key(seq: object, move: dict) -> str:
    """Key a retry to both the turn cursor and the exact submitted move."""
    payload = {
        key: value
        for key, value in move.items()
        if key not in {"idempotency_key", "memo"}
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{seq}-{digest}"


def extract_json_object(text: str) -> dict | None:
    """Return the first complete top-level JSON object in mixed model output."""
    decoder = json.JSONDecoder()
    idx = (text or "").find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None


# ── liars_dice ──────────────────────────────────────────────────────────────

def matches_face(die: int, face: int) -> bool:
    """1s are wild for non-1 faces; bids ON 1s count only real 1s."""
    return die == face or (die == 1 and face != 1)


def prob_bid_true(quantity: int, face: int, my_dice: list[int], total_dice: int) -> float:
    """P(at least `quantity` dice of `face` across ALL dice), given my hand.

    Each unknown die matches a non-1 face with p = 2/6 (the face itself or a
    wild 1) and face 1 with p = 1/6.
    """
    mine = sum(1 for die in my_dice if matches_face(die, face))
    need = quantity - mine
    if need <= 0:
        return 1.0
    unknown = max(0, total_dice - len(my_dice))
    if need > unknown:
        return 0.0
    p = (1 / 6) if face == 1 else (2 / 6)
    return sum(
        comb(unknown, i) * (p ** i) * ((1 - p) ** (unknown - i))
        for i in range(need, unknown + 1)
    )


def liars_analysis(state: dict) -> dict | None:
    """Numbers for the prompt: how plausible is the standing bid, and what are
    my strongest raises? Returns None outside liars_dice."""
    my_dice = state.get("your_dice") or []
    total = state.get("total_dice_count") or 0
    if not my_dice or not total:
        return None
    last_bid = state.get("last_bid") or {}
    analysis: dict = {
        "my_face_counts_with_wilds": {
            str(face): sum(1 for die in my_dice if matches_face(die, face)) for face in range(1, 7)
        },
    }
    if last_bid:
        quantity, face = int(last_bid.get("quantity", 1)), int(last_bid.get("face", 2))
        analysis["p_standing_bid_true"] = round(prob_bid_true(quantity, face, my_dice, total), 3)
        raises = {}
        for candidate in range(1, 7):
            # strictly higher = quantity+1 any face, or same quantity higher face
            if FACE_RANK[candidate] > FACE_RANK[face]:
                raises[f"{quantity}x{candidate}"] = round(prob_bid_true(quantity, candidate, my_dice, total), 3)
            raises[f"{quantity + 1}x{candidate}"] = round(
                prob_bid_true(quantity + 1, candidate, my_dice, total), 3)
        analysis["p_my_raise_candidates_true"] = raises
    return analysis


# ── las_vegas ───────────────────────────────────────────────────────────────

def score_faces(entries: list) -> list[dict]:
    """Score each faces_available entry: what my placement is worth given the
    tie-cancellation rule (EQUAL counts cancel to zero — tying a leader can be
    worth more than winning a small casino). Higher score = better."""
    scored = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        bills = sorted((entry.get("casino_bills") or []), reverse=True)
        top_bill = bills[0] if bills else 0
        second_bill = bills[1] if len(bills) > 1 else 0
        mine_after = (entry.get("your_dice_already_there") or 0) + (entry.get("dice_you_would_place") or 0)
        others = [c for player, c in (entry.get("casino_dice_by_player") or {}).items()]
        best_other = max(others) if others else 0

        if mine_after > best_other:
            score, outcome = top_bill, "leading"
        elif mine_after == best_other and best_other > 0:
            # Mutual wipe: I take nothing but DENY the leader the top bill.
            score, outcome = 0.4 * top_bill, "tie_kill"
        else:
            score, outcome = 0.2 * second_bill, "behind"
        scored.append({
            "face": entry.get("face"), "score": round(score, 1), "outcome": outcome,
            "dice_committed": entry.get("dice_you_would_place"),
            "casino_top_bill": top_bill,
        })
    return sorted(scored, key=lambda item: item["score"], reverse=True)


# ── monopoly ────────────────────────────────────────────────────────────────

def trade_from_opening(hint: dict) -> dict | None:
    """Turn the server's best trade opening into ready propose_trade params.
    Openings carry a suggested_action dict whose extra keys ARE the params."""
    for opening in (hint or {}).get("server_trade_openings") or []:
        suggestion = opening.get("suggested_action") if isinstance(opening, dict) else None
        if isinstance(suggestion, dict) and suggestion.get("action") == "propose_trade":
            return {k: v for k, v in suggestion.items() if k != "action"}
    return None
