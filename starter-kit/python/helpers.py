"""Per-game math the LLM is bad at — computed here, fed to both the heuristic
and (as numbers in the prompt) your model. Pure stdlib, all deterministic.

    liars_dice : prob_bid_true()      wilds-aware binomial over unknown dice
    las_vegas  : score_faces()        EV + tie-cancellation scoring per face
    monopoly   : trade_from_opening() server trade opening -> legal params
    diplomacy  : diplomacy_batch_problems()  hint-contract validation of a batch
                 degrade_diplomacy_batch()   per-order safe fallback for a bad batch
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

def _trade_int(value, *, minimum=0):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def server_trade_openings(hint: dict) -> list[dict]:
    """Return canonical, engine-shaped server openings.

    Server JSON can carry ids as strings or integers. Canonicalizing that
    boundary once prevents a valid current opening from failing exact-match
    validation, while rejecting stale/partial suggestions that would fail the
    engine's two-sided-asset contract.
    """
    result = []
    for opening in (hint or {}).get("server_trade_openings") or []:
        suggestion = opening.get("suggested_action") if isinstance(opening, dict) else None
        if not isinstance(suggestion, dict) or suggestion.get("action") != "propose_trade":
            continue
        to_agent_id = _trade_int(suggestion.get("to_agent_id"), minimum=1)
        cash = {
            key: _trade_int(suggestion.get(key, 0))
            for key in ("offer_cash", "request_cash", "offer_jail_cards", "request_jail_cards")
        }
        try:
            spaces = {
                key: [int(value) for value in (suggestion.get(key) or [])]
                for key in ("offer_space_ids", "request_space_ids")
            }
        except (TypeError, ValueError):
            continue
        if to_agent_id is None or any(value is None for value in cash.values()):
            continue
        if len(set(spaces["offer_space_ids"])) != len(spaces["offer_space_ids"]):
            continue
        if len(set(spaces["request_space_ids"])) != len(spaces["request_space_ids"]):
            continue
        if set(spaces["offer_space_ids"]) & set(spaces["request_space_ids"]):
            continue
        params = {"to_agent_id": to_agent_id, **cash, **spaces}
        offer_has_asset = (
            params["offer_cash"] > 0
            or bool(params["offer_space_ids"])
            or params["offer_jail_cards"] > 0
        )
        request_has_asset = (
            params["request_cash"] > 0
            or bool(params["request_space_ids"])
            or params["request_jail_cards"] > 0
        )
        if offer_has_asset and request_has_asset:
            result.append(params)
    return result


def trade_from_opening(hint: dict) -> dict | None:
    """Turn the server's best valid opening into ready trade params."""
    openings = server_trade_openings(hint)
    return dict(openings[0]) if openings else None


_MONOPOLY_MANAGEMENT_ACTIONS = {
    "build_house",
    "sell_house",
    "mortgage",
    "unmortgage",
}


def server_manage_batch_params(hint: dict) -> dict | None:
    """Return one canonical server-authored Clawpoly management batch.

    The server computes this plan against the same current engine snapshot that
    authored ``legal_actions``. Defensively normalize integer ids/counts here so
    a stale or placeholder model batch is never submitted merely because its
    outer action name is legal.
    """
    binding = (hint or {}).get("server_manage_batch")
    params = binding.get("params") if isinstance(binding, dict) else None
    operations = params.get("operations") if isinstance(params, dict) else None
    if not isinstance(operations, list) or not operations:
        return None
    canonical = []
    for operation in operations:
        if not isinstance(operation, dict):
            return None
        action = str(operation.get("action") or "")
        if action == "end_turn":
            canonical.append({"action": "end_turn"})
            continue
        if action not in _MONOPOLY_MANAGEMENT_ACTIONS:
            return None
        try:
            space_id = int(operation.get("space_id"))
        except (TypeError, ValueError):
            return None
        if space_id < 0:
            return None
        normalized = {"action": action, "space_id": space_id}
        if action in {"build_house", "sell_house"}:
            try:
                count = int(operation.get("count", 1))
            except (TypeError, ValueError):
                return None
            if count < 1:
                return None
            normalized["count"] = count
        canonical.append(normalized)
    return {"operations": canonical}


# ── diplomacy ───────────────────────────────────────────────────────────────
# One shared hint-contract validator for the offline fixture harness
# (check.py) AND the live decision path (llm_agent.py). A hallucinated support
# pair or coast-less build resolves as a wasted INVALID order on the server,
# so both surfaces must agree on what "hint-legal" means.

_DIPLOMACY_SPLIT_COASTS = {
    "BUL": {"BUL/EC", "BUL/SC"},
    "SPA": {"SPA/NC", "SPA/SC"},
    "STP": {"STP/NC", "STP/SC"},
}

_DIPLOMACY_BATCH_ACTIONS = {
    "send_press",
    "submit_orders",
    "submit_retreats",
    "submit_adjustments",
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


def _diplomacy_allowed_ids(hint: dict, key: str) -> set[str] | None:
    if key not in hint:
        return None
    return {
        str(value).strip().upper()
        for value in (hint.get(key) or [])
        if str(value).strip()
    }


def _diplomacy_id_list_problem(
    value,
    *,
    field: str,
    limit: int,
    allowed: set[str] | None,
    province: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return f"{field} must be an array"
    if len(value) > limit:
        return f"{field} cannot contain more than {limit} entries"
    for index, item in enumerate(value):
        normalized = _diplomacy_base(item) if province else (
            str(item).strip().upper() if isinstance(item, str) else None
        )
        if not normalized:
            return f"{field}[{index}] is not a canonical identifier"
        if allowed is not None and normalized not in allowed:
            return f"{field}[{index}] {item!r} is not in the server-authorized ids"
    return None


def _diplomacy_proposal_order_problem(order, *, field: str, province_ids) -> str | None:
    if not isinstance(order, dict):
        return f"{field} must be an object"
    allowed_fields = {
        "type", "origin", "destination", "location", "target", "unit_type", "via_convoy",
    }
    unknown = sorted(set(order) - allowed_fields)
    if unknown:
        return f"{field}.{unknown[0]} is not supported"
    kind = str(order.get("type") or "").strip().upper()
    if kind not in {"HOLD", "MOVE", "SUPPORT", "CONVOY"}:
        return f"{field}.type must be HOLD, MOVE, SUPPORT, or CONVOY"
    required = {
        "HOLD": ("origin",),
        "MOVE": ("origin", "destination"),
        "SUPPORT": ("origin", "target"),
        "CONVOY": ("origin", "target", "destination"),
    }[kind]
    for name in required:
        value = order.get(name)
        normalized = _diplomacy_base(value)
        if not normalized:
            return f"{field}.{name} must be a canonical province id"
        if province_ids is not None and normalized not in province_ids:
            return f"{field}.{name} {value!r} is not in valid_province_ids"
    if "via_convoy" in order and not isinstance(order.get("via_convoy"), bool):
        return f"{field}.via_convoy must be a boolean"
    return None


def _diplomacy_proposal_problem(message: dict, *, index: int, hint: dict) -> str | None:
    proposal = message.get("proposal")
    if proposal in (None, {}):
        return None
    field = f"diplomacy messages[{index}].proposal"
    if not isinstance(proposal, dict):
        return f"{field} must be an object"
    allowed_fields = {
        "kind", "status", "reply_to", "terms", "provinces", "offers", "requests",
    }
    unknown = sorted(set(proposal) - allowed_fields)
    if unknown:
        return f"{field}.{unknown[0]} is not supported"
    kind = str(proposal.get("kind") or "").strip().lower()
    kinds = {"support", "dmz", "non_aggression", "coordinated_move", "convoy", "information"}
    if kind not in kinds:
        return f"{field}.kind is not server-supported"
    status = str(proposal.get("status") or "open").strip().lower()
    if status not in {"open", "accept", "reject", "counter"}:
        return f"{field}.status is not server-supported"
    reply_to = str(proposal.get("reply_to") or "").strip()
    reply_targets = hint.get("proposal_reply_targets") or {}
    if status != "open":
        if not reply_to:
            return f"{field}.reply_to is required when status is {status}"
        if "valid_proposal_ids" in hint and reply_to not in set(hint.get("valid_proposal_ids") or []):
            return f"{field}.reply_to {reply_to!r} is not in valid_proposal_ids"
        expected_target = str(reply_targets.get(reply_to) or "").upper()
        target = str(message.get("to_power") or "").upper()
        if expected_target and target != expected_target:
            return f"{field}.reply_to must be sent to {expected_target}"
    terms = proposal.get("terms")
    if terms is not None and (not isinstance(terms, str) or len(terms.strip()) > 400):
        return f"{field}.terms must be a string of at most 400 characters"
    province_ids = _diplomacy_allowed_ids(hint, "valid_province_ids")
    problem = _diplomacy_id_list_problem(
        proposal.get("provinces"),
        field=f"{field}.provinces",
        limit=8,
        allowed=province_ids,
        province=True,
    )
    if problem:
        return problem
    for name in ("offers", "requests"):
        orders = proposal.get(name)
        if orders is None:
            continue
        if not isinstance(orders, list) or len(orders) > 4:
            return f"{field}.{name} must be an array with at most 4 entries"
        for order_index, order in enumerate(orders):
            problem = _diplomacy_proposal_order_problem(
                order,
                field=f"{field}.{name}[{order_index}]",
                province_ids=province_ids,
            )
            if problem:
                return problem
    return None


def _diplomacy_strategy_problem(params: dict, hint: dict) -> str | None:
    intent = params.get("strategy_intent")
    if intent in (None, {}):
        return None
    if not isinstance(intent, dict):
        return "strategy_intent must be an object"
    allowed_fields = {
        "objective", "risk_posture", "priority_targets", "avoid_provinces", "dmz_provinces",
        "trusted_powers", "threat_powers", "accepted_proposal_ids", "rejected_proposal_ids",
        "planned_candidate_id",
    }
    unknown = sorted(set(intent) - allowed_fields)
    if unknown:
        return f"strategy_intent.{unknown[0]} is not supported"
    objective = intent.get("objective")
    if objective is not None and (not isinstance(objective, str) or len(objective.strip()) > 400):
        return "strategy_intent.objective must be a string of at most 400 characters"
    risk = intent.get("risk_posture")
    if risk not in (None, "") and str(risk).strip().lower() not in {
        "conservative", "balanced", "aggressive",
    }:
        return "strategy_intent.risk_posture is not server-supported"
    province_ids = _diplomacy_allowed_ids(hint, "valid_province_ids")
    for name in ("priority_targets", "avoid_provinces", "dmz_provinces"):
        problem = _diplomacy_id_list_problem(
            intent.get(name),
            field=f"strategy_intent.{name}",
            limit=8,
            allowed=province_ids,
            province=True,
        )
        if problem:
            return problem
    priority = {str(value).upper() for value in (intent.get("priority_targets") or [])}
    avoided = {str(value).upper() for value in (intent.get("avoid_provinces") or [])}
    dmz = {str(value).upper() for value in (intent.get("dmz_provinces") or [])}
    if priority & (avoided | dmz):
        return "strategy_intent cannot prioritize a province it also avoids or treats as a DMZ"
    power_ids = _diplomacy_allowed_ids(hint, "valid_power_ids")
    for name in ("trusted_powers", "threat_powers"):
        problem = _diplomacy_id_list_problem(
            intent.get(name),
            field=f"strategy_intent.{name}",
            limit=6,
            allowed=power_ids,
        )
        if problem:
            return problem
    trusted = {str(value).upper() for value in (intent.get("trusted_powers") or [])}
    threats = {str(value).upper() for value in (intent.get("threat_powers") or [])}
    if trusted & threats:
        return "strategy_intent cannot mark one power as both trusted and threatening"
    proposal_ids = (
        set(hint.get("valid_proposal_ids") or [])
        if "valid_proposal_ids" in hint
        else None
    )
    for name in ("accepted_proposal_ids", "rejected_proposal_ids"):
        problem = _diplomacy_id_list_problem(
            intent.get(name),
            field=f"strategy_intent.{name}",
            limit=16,
            allowed=proposal_ids,
        )
        if problem:
            return problem
    accepted = set(intent.get("accepted_proposal_ids") or [])
    rejected = set(intent.get("rejected_proposal_ids") or [])
    if accepted & rejected:
        return "strategy_intent cannot both accept and reject one proposal"
    candidate = intent.get("planned_candidate_id")
    if candidate not in (None, ""):
        if not isinstance(candidate, str) or len(candidate.strip()) > 64:
            return "strategy_intent.planned_candidate_id must be a string of at most 64 characters"
        if "valid_candidate_ids" in hint and candidate not in set(hint.get("valid_candidate_ids") or []):
            return "strategy_intent.planned_candidate_id is not in valid_candidate_ids"
    return None


def diplomacy_server_fallback(action: str, legal_actions: list[dict]) -> dict | None:
    """Return the exact safe fallback authored by the current server contract."""
    for entry in legal_actions or []:
        if not isinstance(entry, dict) or entry.get("action") != action:
            continue
        hint = entry.get("hint") if isinstance(entry.get("hint"), dict) else {}
        fallback = hint.get("server_fallback") if isinstance(hint, dict) else None
        params = fallback.get("params") if isinstance(fallback, dict) else None
        if isinstance(params, dict):
            return {"action": action, "params": dict(params)}
    return None


def diplomacy_batch_problems(action: str, params: dict, hint: dict) -> list[str]:
    """Validate the structured batch against the server's machine hints.

    This intentionally checks the wire contract, not strategy: MOVE/SUPPORT/
    CONVOY orders are just as valid as HOLD when their required fields and
    hinted origins/destinations line up.
    """
    params = params if isinstance(params, dict) else {}
    hint = hint if isinstance(hint, dict) else {}
    if action == "send_press":
        unknown = sorted(set(params) - {"messages", "strategy_intent", "use_server_default"})
        if unknown:
            return [f"diplomacy params.{unknown[0]} is not supported for send_press"]
        use_server_default = params.get("use_server_default", False)
        if not isinstance(use_server_default, bool):
            return ["diplomacy use_server_default must be a boolean"]
        if use_server_default:
            conflicting = [
                key
                for key in ("messages", "strategy_intent")
                if params.get(key) not in (None, [], {})
            ]
            if conflicting:
                return [
                    "diplomacy use_server_default conflicts with press messages or strategy intent"
                ]
            fallback = hint.get("server_fallback") or {}
            if (fallback.get("params") or {}).get("use_server_default") is not True:
                return ["diplomacy server default is not authorized by the current hint"]
            return []
        messages = params.get("messages", [])
        if not isinstance(messages, list):
            return [f"diplomacy messages must be an array, got {messages!r}"]
        raw_limit = hint.get("max_messages", 7)
        try:
            max_messages = max(0, int(raw_limit))
        except (TypeError, ValueError):
            max_messages = 7
        if len(messages) > max_messages:
            return [
                f"diplomacy press batch exceeds {max_messages} messages: {len(messages)}"
            ]
        recipients = set(hint.get("recipient_powers") or []) | {"global"}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                return [f"diplomacy messages[{index}] must be an object"]
            unknown_fields = sorted(set(message) - {"to_power", "content", "proposal"})
            if unknown_fields:
                return [f"diplomacy messages[{index}].{unknown_fields[0]} is not supported"]
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
            proposal_problem = _diplomacy_proposal_problem(message, index=index, hint=hint)
            if proposal_problem:
                return [proposal_problem]
        strategy_problem = _diplomacy_strategy_problem(params, hint)
        if strategy_problem:
            return [strategy_problem]
        return []

    use_server_default = params.get("use_server_default", False)
    if not isinstance(use_server_default, bool):
        return ["diplomacy use_server_default must be a boolean"]
    if use_server_default:
        conflicting = [
            key
            for key in ("orders", "candidate_id", "order_overrides")
            if params.get(key) not in (None, "", [])
        ]
        if conflicting:
            return ["diplomacy use_server_default conflicts with explicit orders or candidates"]
        fallback = hint.get("server_fallback") or {}
        if (fallback.get("params") or {}).get("use_server_default") is not True:
            return ["diplomacy server default is not authorized by the current hint"]
        return []

    unknown = sorted(set(params) - {"orders", "candidate_id", "order_overrides", "use_server_default"})
    if unknown:
        return [f"diplomacy params.{unknown[0]} is not supported for {action}"]

    candidate_id = str(params.get("candidate_id") or "").strip()
    if candidate_id:
        if params.get("orders") not in (None, []):
            return ["diplomacy candidate_id and orders are mutually exclusive"]
        overrides = params.get("order_overrides", [])
        if not isinstance(overrides, list) or len(overrides) > 2:
            return ["diplomacy order_overrides must be an array with at most 2 entries"]
        advice = hint.get("heuristic_advice") or {}
        candidate = next(
            (
                item
                for item in (advice.get("candidates") or [])
                if isinstance(item, dict) and item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if candidate is None:
            return [f"diplomacy candidate_id {candidate_id!r} is not in current advice"]
        candidate_orders = [
            dict(order)
            for order in (candidate.get("orders") or [])
            if isinstance(order, dict)
        ]
        by_origin = {
            _diplomacy_base(order.get("origin")): index
            for index, order in enumerate(candidate_orders)
            if _diplomacy_base(order.get("origin"))
        }
        replaced = set()
        for index, override in enumerate(overrides):
            if not isinstance(override, dict):
                return [f"diplomacy order_overrides[{index}] must be an object"]
            origin = _diplomacy_base(override.get("origin"))
            if origin is None or origin not in by_origin:
                return [
                    f"diplomacy order_overrides[{index}] must replace a candidate origin"
                ]
            if origin in replaced:
                return [f"diplomacy order_overrides duplicate origin {origin!r}"]
            replaced.add(origin)
            candidate_orders[by_origin[origin]] = dict(override)
        return diplomacy_batch_problems(
            action,
            {"orders": candidate_orders},
            hint,
        )

    orders = params.get("orders")
    if not isinstance(orders, list):
        return [f"diplomacy orders must be an array, got {orders!r}"]
    if any(not isinstance(order, dict) for order in orders):
        return ["every diplomacy order must be an object"]

    legal_orders = [entry for entry in (hint.get("legal_orders") or []) if isinstance(entry, dict)]
    shared_candidates = hint.get("shared_candidates") or {}
    by_origin = {}
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


def degrade_diplomacy_batch(action: str, params: dict, hint: dict) -> tuple[dict, list[str]]:
    """Coerce an invalid diplomacy batch into the nearest hint-legal batch.

    Every entry is re-validated incrementally with diplomacy_batch_problems;
    entries that pass are forwarded untouched. An offending order degrades to
    the phase's deadline-safe default — movement HOLD, retreat DISBAND,
    adjustment WAIVE — and entries with no legal fallback (unknown/duplicate
    origins, malformed press, forced-removal disbands) are dropped so the
    server default covers them. Returns (safe_params, notes); empty notes
    means the batch was already valid.
    """
    params = params if isinstance(params, dict) else {}
    notes: list[str] = []
    if action == "send_press":
        if params.get("use_server_default") is True:
            fallback = (hint.get("server_fallback") or {}).get("params")
            if isinstance(fallback, dict):
                return dict(fallback), notes
        raw_messages = params.get("messages")
        raw_messages = raw_messages if isinstance(raw_messages, list) else []
        accepted: list = []
        for index, message in enumerate(raw_messages):
            if not diplomacy_batch_problems(action, {"messages": accepted + [message]}, hint):
                accepted.append(message)
                continue
            without_proposal = (
                {key: value for key, value in message.items() if key != "proposal"}
                if isinstance(message, dict)
                else message
            )
            if not diplomacy_batch_problems(
                action,
                {"messages": accepted + [without_proposal]},
                hint,
            ):
                accepted.append(without_proposal)
                notes.append(f"messages[{index}] kept with invalid proposal metadata removed")
            else:
                notes.append(f"messages[{index}] dropped: failed hint validation")
        safe = {"messages": accepted}
        if params.get("strategy_intent") not in (None, {}):
            with_intent = {**safe, "strategy_intent": params.get("strategy_intent")}
            if not diplomacy_batch_problems(action, with_intent, hint):
                safe["strategy_intent"] = params["strategy_intent"]
            else:
                notes.append("strategy_intent removed: failed server contract validation")
        return safe, notes

    if params.get("use_server_default") is True:
        fallback = (hint.get("server_fallback") or {}).get("params")
        if isinstance(fallback, dict):
            return dict(fallback), notes

    if params.get("candidate_id"):
        advice = hint.get("heuristic_advice") or {}
        candidate_ids = {
            str(item.get("candidate_id") or "")
            for item in (advice.get("candidates") or [])
            if isinstance(item, dict)
        }
        if str(params.get("candidate_id")) not in candidate_ids:
            fallback = (hint.get("server_fallback") or {}).get("params")
            if isinstance(fallback, dict):
                notes.append("unknown candidate replaced with server fallback")
                return dict(fallback), notes

    raw_orders = params.get("orders")
    raw_orders = raw_orders if isinstance(raw_orders, list) else []
    fallback_type = {
        "submit_orders": "HOLD",
        "submit_retreats": "DISBAND",
        "submit_adjustments": "WAIVE",
    }.get(action)
    accepted = []
    for index, order in enumerate(raw_orders):
        if not diplomacy_batch_problems(action, {"orders": accepted + [order]}, hint):
            accepted.append(order)
            continue
        label = order.get("type") if isinstance(order, dict) else order
        fallback = None
        if fallback_type == "WAIVE":
            fallback = {"type": "WAIVE"}
        elif fallback_type and isinstance(order, dict):
            origin = order.get("origin")
            if isinstance(origin, str) and origin.strip():
                fallback = {"type": fallback_type, "origin": origin}
        if fallback is not None and not diplomacy_batch_problems(
            action, {"orders": accepted + [fallback]}, hint
        ):
            accepted.append(fallback)
            notes.append(f"orders[{index}] ({label!r}) degraded to {fallback_type}")
        else:
            notes.append(f"orders[{index}] ({label!r}) dropped: no legal fallback")
    return {"orders": accepted}, notes
