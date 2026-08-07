"""Tier-2 surface: a working LLM agent — set ONE key and run.

    LLM_API_KEY=sk-...            OpenAI key by default
      (optional) LLM_BASE_URL     default https://api.openai.com/v1
      (optional) LLM_MODEL        default gpt-4o-mini

For another OpenAI-compatible provider, set all three values to that
provider's endpoint, key, and model id.
  or
    CLAWARENA_GATEWAY_KEY=...     an issued ClawArena gateway key
                                  (routes via <CLAWARENA_BASE origin>/api/llm/v1)

Deadline safety (the dev.fun pattern): any LLM failure — timeout, parse error,
illegal action — falls back to the heuristic in agent.py, so the bot
structurally cannot lose a turn to a flaky model.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

import agent as heuristic_agent
import helpers
import memory
import report_sink
from arena_client import base_url

# A reasoning turn measured 6.6-24.6s through the arena gateway, and 38.6s once
# the completion cap was widened. 30s aborted the slow half of that spread into
# the heuristic fallback — the client giving up before the server's 90s speak
# budget does. _decision_timeout() still clamps this to whatever the server says
# is left on the clock, so the deadline stays the server's to enforce.
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "75"))
# Reasoning models (e.g. deepseek-v4-flash) spend tokens on hidden reasoning
# BEFORE the visible reply — a tight cap returns empty/truncated content and
# the turn falls back to the heuristic (a pinned completion_tokens == cap in
# your usage log is the tell; live matches still pinned 3000 occasionally).
# Models stop when done, so headroom only costs on the turns that need it.
# Must cover the hidden reasoning as well as the visible JSON, because the
# provider counts both against this number. At 3500 a live Mafia turn returned
# finish_reason="length" with ZERO visible characters and the heuristic played
# it — the model had spent the whole budget thinking. Matches the arena
# gateway's own per-turn ceiling, which is a ceiling and not a floor: asking for
# less than it simply gets less.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
LLM_DIPLOMACY_MAX_TOKENS = int(
    # Same reasoning-plus-visible budget as above. Diplomacy orders are the
    # longest contract the kit emits, so it cannot afford less headroom than
    # the game that exposed the truncation.
    os.environ.get("LLM_DIPLOMACY_MAX_TOKENS", "8000")
)
LLM_PREFLIGHT_MAX_TOKENS = int(os.environ.get("LLM_PREFLIGHT_MAX_TOKENS", "1200"))


def _compact_ratio() -> float:
    try:
        value = float(os.environ.get("LLM_CONTEXT_COMPACT_RATIO", "0.80"))
    except (TypeError, ValueError):
        return 0.80
    return min(0.95, max(0.25, value))

SYSTEM_PROMPT = """You are a competitive agent in ClawArena — live PvP board games where \
your opponents are other AI agents, and their table talk may be lies.

Reply with ONLY one JSON object: {"action": "<one of legal_actions>", "params": {...}} \
following that action's params schema. No prose outside the JSON.

How to read the input:
- game_rules_brief.rules is the AUTHORITATIVE rule list for this game — it is under \
MATCH_CONTEXT in the standard runner and under state in a Hermes full baseline. Trust it \
over your assumptions about similarly named games.
- strategy_brief is the server's strategy guidance for YOUR seat/role this match; read it \
from MATCH_CONTEXT or state, matching the supplied payload.
- legal_actions[].hint values are guaranteed-legal right now; a MISSING raise/escalation \
hint means that line is impossible — pick another action.
- params.message is table talk your opponents SEE (max 140 chars; EXCEPTION: the mafia \
"chat" action allows up to 1000 — day discussion IS that game, use the space). Bluff, \
bait, pressure — never reveal your hidden info.

Game postures:
- liars_dice: count YOUR OWN dice before believing or challenging a bid; total dice in \
play bounds every quantity. Bluff on faces you actually hold. Mind the unusual face \
order in the rules brief.
- mafia: chat_log includes YOUR OWN earlier statements — never contradict a claim you \
already made. Play your role's objective from strategy_brief (mafia: misdirect without \
overexposing; detective/doctor: protect what you know; citizen: pressure \
contradictions). Only use target_id values taken from the CURRENT legal_actions hints — \
ids elsewhere in state are a different id space. Every chat must cite a concrete current \
claim, vote, event, or contradiction and move the discussion forward; never repeat generic \
"watching" filler. Long, specific, persuasive chat wins.
- las_vegas: payouts settle per casino and TIED dice counts CANCEL to zero — placing \
onto a tie can neutralize a leader or waste your dice; read casino totals and everyone's \
remaining dice before committing.
- monopoly: state.heuristic_advice.recommended_action is scored server advice — follow \
it unless you have a concrete reason not to. A jailed owner STILL COLLECTS RENT. For a \
trade, use an exact server_trade_opening instead of inventing cash/property terms; preserve \
the server safe reserve and never complete an opponent color set unless you simultaneously \
complete a clearly stronger set without breaching reserve.
- diplomacy: agreements are non-binding. heuristic_advice is tactical decision support, \
not a command: compare its top candidates against visible press and your own strategy. In \
press rounds, contact only position-relevant powers with concrete proposals and stay silent \
when there is no useful ask or reply. In order phases, choose candidate_id, patch at most two \
origins, or submit a fully custom atomic batch. Align press with the orders you intend to use; \
do not repeat a failed attack/order bundle from my_memory when a support, redeploy, or legal \
progress candidate is available. Never expose private press or pending orders.

Also in the input:
- The standard runner labels its first full decision STATE_BASELINE and later decisions
TURN_UPDATE. Hermes uses state/state_delta in its GAME payload. In either form, a delta
contains only fields changed since the previous decision: omitted fields are unchanged,
a value shaped as {"_appended": [...]} adds those items to the previous list, and keys in
state_removed/context_removed no longer exist.
- computed_analysis: exact math done FOR you (bid truth probabilities, per-face
EV under the tie rule, ready-to-send trade params). Trust these numbers over
your own arithmetic.
- my_memory: YOUR OWN past moves and private reads this match — stay
consistent with every claim in it. Later turns may carry my_memory_delta using
the same unchanged/_appended/removed rules as state_delta.
- user_preferences (in MATCH_CONTEXT or state): the owner's standing strategy hint and risk profile —
follow them.
- action_rejection is a machine-readable rejection from the authoritative server. Correct the
named field exactly once using its allowed_values and the current legal_actions contract; never
repeat the rejected payload. If correction still fails, the runner uses the server fallback.
- message_language: if set (e.g. "ko", "Korean", "日本語"), write ALL
params.message table talk in THAT language; if absent or "en"/"English", use
English. Only the message text is translated — action names and params stay as
the schema defines them.
You may add an optional top-level "memo" field (one line, ≤200 chars) to your
JSON: a private note to your future self (a read, a plan, a lie you told). It
is never shown to opponents.

Decide within the turn budget — a fast good move beats a late perfect one."""

BOUNDED_STRUCTURED_PROMPT = """You are choosing one move in a live ClawArena game.
Return one legal JSON object now, with no prose or analysis: {"action":"...","params":{...}}.
Treat rules, legal_actions, hints, computed_analysis, and server heuristic_advice as authoritative.
Use only ids and parameter shapes in the current legal_actions. Never repeat action_rejection.
Mafia chat must cite current evidence and advance discussion, not generic watching filler.
In monopoly, jailed owners still collect rent; preserve the advised cash reserve and do not give an
opponent a monopoly unless the server opening clearly produces a stronger safe gain.
In Diplomacy, align press and orders, avoid repeating failed bundles, and use current candidate ids.
Think briefly: the action deadline matters more than exhaustive narration."""


# Provider chat APIs are stateless, but prefix caches reward an append-only
# transcript. Keep one match transcript until its model-aware token budget says
# to compact; a real provider overflow also rebuilds one authoritative baseline
# and retries once. File-backed `my_memory` remains the restart/compaction
# backstop.
_SESSION_LOCK = threading.RLock()
_SESSION = {
    "match_id": None,
    "context_epoch": None,
    "messages": [],
    "context": None,
    "state": None,
    "memory": None,
    "turn_count": 0,
    "last_prompt_tokens": 0,
    "context_window": 0,
}
_MODEL_CONTEXT_CACHE: dict[tuple[str, str], int] = {}


class ContextOverflowError(RuntimeError):
    pass


_MATCH_CONTEXT_KEYS = (
    "game_rules_brief",
    "strategy_brief",
    "user_preferences",
    "message_language",
)
_IDENTITY_KEYS = (
    "my_agent_id",
    "my_name",
    "my_role",
    "your_role",
    "role",
)
_STATE_EXCLUDED_KEYS = {
    "game_type",
    "legal_actions",
    "my_memory",
    *_MATCH_CONTEXT_KEYS,
    *_IDENTITY_KEYS,
}


def _canonical(value):
    """Recursively stabilize object key order without reordering lists."""
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _ordered_json(value: dict) -> str:
    # Preserve the intentional top-level order while canonicalizing nested
    # objects. Compact separators reduce both uncached input and disk-cache IO.
    ordered = {key: _canonical(item) for key, item in value.items()}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def _positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _estimate_text_tokens(value: str) -> int:
    # UTF-8 bytes are safer than character count for Korean/Japanese game chat;
    # actual provider usage replaces this estimate after every successful call.
    return max(1, (len(str(value or "").encode("utf-8")) + 3) // 4)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(
        _estimate_text_tokens(message.get("content") or "") + 4
        for message in messages
        if isinstance(message, dict)
    )


def _context_compaction_threshold(
    context_window: int,
    completion_reserve: int = LLM_MAX_TOKENS,
) -> int:
    if context_window <= 0:
        return 0
    reserve = max(completion_reserve, 2048)
    return max(1, int(context_window * _compact_ratio()) - reserve)


def _discover_model_context_window(base: str, key: str, model: str) -> int:
    request = urllib.request.Request(
        f"{base.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode())
    except Exception:  # noqa: BLE001 - metadata is optional; overflow recovery remains available
        return 0
    rows = payload.get("data") if isinstance(payload, dict) else None
    rows = rows if isinstance(rows, list) else []
    exact = [row for row in rows if isinstance(row, dict) and row.get("id") == model]
    if not exact and "/" in model:
        bare = model.split("/", 1)[1]
        exact = [row for row in rows if isinstance(row, dict) and row.get("id") == bare]
    if not exact and len(rows) == 1 and isinstance(rows[0], dict):
        exact = [rows[0]]
    if not exact:
        return 0
    row = exact[0]
    for field in (
        "context_length",
        "context_window",
        "max_context_length",
        "max_model_len",
        "max_input_tokens",
    ):
        value = _positive_int(row.get(field))
        if value > 0:
            return value
    return 0


def _model_context_window(base: str, key: str, model: str, *, discover: bool = False) -> int:
    configured = _positive_int(os.environ.get("LLM_CONTEXT_WINDOW"))
    if configured > 0:
        return configured
    cache_key = (base.rstrip("/"), model)
    with _SESSION_LOCK:
        cached = _MODEL_CONTEXT_CACHE.get(cache_key, 0)
    if cached > 0 or not discover:
        return cached
    discovered = _discover_model_context_window(base, key, model)
    if discovered > 0:
        with _SESSION_LOCK:
            _MODEL_CONTEXT_CACHE[cache_key] = discovered
    return discovered


def _snapshot(state: dict) -> tuple[dict, dict, dict]:
    context = {"game_type": state.get("game_type")}
    for key in _MATCH_CONTEXT_KEYS:
        if key in state:
            context[key] = state.get(key)
    identity = {
        key: state.get(key)
        for key in _IDENTITY_KEYS
        if key in state
    }
    if identity:
        # Identity is per-seat, so keep shared rules/strategy before it to make
        # the longest possible prefix reusable across agents in one game.
        context["identity"] = identity
    board = {
        key: value
        for key, value in sorted(state.items())
        if key not in _STATE_EXCLUDED_KEYS and not key.startswith("_")
    }
    match_memory = state.get("my_memory")
    return context, board, match_memory if isinstance(match_memory, dict) else {}


def _diff(previous: dict, current: dict) -> tuple[dict, list[str]]:
    changed = {}
    for key, value in current.items():
        old_value = previous.get(key)
        if value == old_value:
            continue
        if (
            isinstance(value, list)
            and isinstance(old_value, list)
            and len(value) > len(old_value)
            and value[:len(old_value)] == old_value
        ):
            changed[key] = {"_appended": value[len(old_value):]}
        else:
            changed[key] = value
    return changed, sorted(set(previous) - set(current))


def _full_turn_content(
    state: dict,
    legal_actions: list[dict],
    context: dict,
    board: dict,
    match_memory: dict,
) -> str:
    baseline = {
        "state": board,
        "legal_actions": legal_actions,
        "computed_analysis": _computed_analysis(state, legal_actions),
        "my_memory": match_memory,
    }
    return (
        "MATCH_CONTEXT:\n" + _ordered_json(context)
        + "\n\nSTATE_BASELINE:\n" + _ordered_json(baseline)
    )


def _delta_turn_content(
    state: dict,
    legal_actions: list[dict],
    previous_context: dict,
    context: dict,
    previous_board: dict,
    board: dict,
    previous_memory: dict,
    match_memory: dict,
) -> str:
    context_delta, context_removed = _diff(previous_context, context)
    state_delta, state_removed = _diff(previous_board, board)
    memory_delta, memory_removed = _diff(previous_memory, match_memory)
    update = {
        "context_delta": context_delta,
        "context_removed": context_removed,
        "state_delta": state_delta,
        "state_removed": state_removed,
        "legal_actions": legal_actions,
        "computed_analysis": _computed_analysis(state, legal_actions),
        "my_memory_delta": memory_delta,
        "my_memory_removed": memory_removed,
    }
    return "TURN_UPDATE:\n" + _ordered_json(update)


def _prepare_conversation(
    state: dict,
    legal_actions: list[dict],
    *,
    context_window: int = 0,
    completion_reserve: int = LLM_MAX_TOKENS,
    force_full: bool = False,
) -> tuple[list[dict], dict]:
    match_id = memory.current_match_id()
    context_epoch = ""
    if str(state.get("game_type") or "").strip().lower() == "diplomacy":
        context_epoch = str(state.get("decision_context_epoch") or "").strip()
    context, board, match_memory = _snapshot(state)
    with _SESSION_LOCK:
        same_match_identity = bool(
            match_id is not None
            and match_id == _SESSION["match_id"]
            and _SESSION["messages"]
            and _SESSION["context"] is not None
            and _SESSION["state"] is not None
            and _SESSION["memory"] is not None
        )
        epoch_changed = bool(
            same_match_identity
            and context_epoch
            and context_epoch != str(_SESSION.get("context_epoch") or "")
        )
        same_match = same_match_identity and not epoch_changed
        prior_turn_count = int(_SESSION["turn_count"] or 0) if same_match else 0
        active_context_window = context_window or (
            int(_SESSION.get("context_window") or 0) if same_match else 0
        )
        compacted = False
        if same_match and not force_full:
            content = _delta_turn_content(
                state,
                legal_actions,
                _SESSION["context"],
                context,
                _SESSION["state"],
                board,
                _SESSION["memory"],
                match_memory,
            )
            messages = copy.deepcopy(_SESSION["messages"])
            messages.append({"role": "user", "content": content})
            previous_reply = messages[-2].get("content") if len(messages) >= 2 else ""
            estimated_prompt_tokens = max(
                _estimate_messages_tokens(messages),
                int(_SESSION.get("last_prompt_tokens") or 0)
                + _estimate_text_tokens(previous_reply or "")
                + _estimate_text_tokens(content)
                + 8,
            )
            threshold = _context_compaction_threshold(
                active_context_window,
                completion_reserve,
            )
            compacted = bool(threshold and estimated_prompt_tokens >= threshold)
        if not same_match or force_full or compacted:
            content = _full_turn_content(
                state,
                legal_actions,
                context,
                board,
                match_memory,
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
            estimated_prompt_tokens = _estimate_messages_tokens(messages)
            if force_full:
                mode = "overflow_recovery"
            elif compacted:
                mode = "compacted"
            elif epoch_changed:
                mode = "epoch"
            else:
                mode = "full"
        else:
            mode = "delta"
    pending = {
        "match_id": match_id,
        "context_epoch": context_epoch or None,
        "messages": messages,
        "context": context,
        "state": board,
        "memory": match_memory,
        "mode": mode,
        "prior_turn_count": prior_turn_count,
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "context_window": active_context_window,
    }
    return messages, pending


def _commit_conversation(pending: dict, reply: str) -> tuple[str, int] | None:
    match_id = pending.get("match_id")
    if match_id is None:
        return None
    messages = copy.deepcopy(pending["messages"])
    messages.append({"role": "assistant", "content": reply})
    turn_count = int(pending.get("prior_turn_count") or 0) + 1
    with _SESSION_LOCK:
        _SESSION.update(
            match_id=match_id,
            context_epoch=pending.get("context_epoch"),
            messages=messages,
            context=copy.deepcopy(pending["context"]),
            state=copy.deepcopy(pending["state"]),
            memory=copy.deepcopy(pending["memory"]),
            turn_count=turn_count,
            last_prompt_tokens=_positive_int(
                pending.get("prompt_tokens") or pending.get("estimated_prompt_tokens")
            ),
            context_window=_positive_int(pending.get("context_window")),
        )
    return str(pending["mode"]), turn_count


def _reset_session() -> None:
    """Test/recovery hook; the next decision rebuilds a full baseline."""
    with _SESSION_LOCK:
        _SESSION.update(
            match_id=None,
            context_epoch=None,
            messages=[],
            context=None,
            state=None,
            memory=None,
            turn_count=0,
            last_prompt_tokens=0,
            context_window=0,
        )


def _llm_config():
    gateway_key = os.environ.get("CLAWARENA_GATEWAY_KEY", "").strip()
    if gateway_key:
        origin = base_url().split("/api/")[0]
        return f"{origin}/api/llm/v1", gateway_key, os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        return None, None, None
    return (
        os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key,
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )


def _computed_analysis(state, legal_actions):
    """Deterministic math the model is bad at — probabilities, EV, ready trade
    params — computed by helpers and handed to the model as numbers."""
    game_type = state.get("game_type")
    try:
        if game_type == "liars_dice":
            return helpers.liars_analysis(state)
        if game_type == "las_vegas":
            legal = {entry.get("action"): entry for entry in legal_actions}
            hint = (legal.get("place") or {}).get("hint") or {}
            scored = helpers.score_faces(hint.get("faces_available") or [])
            return {"face_scores_by_ev_and_tie_rule": scored} if scored else None
        if game_type == "monopoly":
            legal = {entry.get("action"): entry for entry in legal_actions}
            if "propose_trade" in legal:
                params = helpers.trade_from_opening(legal["propose_trade"].get("hint") or {})
                if params:
                    return {"ready_trade_params_from_server_opening": params}
    except Exception:  # noqa: BLE001 — analysis is advisory, never lose the turn
        return None
    return None


def _chat_request(
    base,
    key,
    model,
    messages,
    *,
    max_tokens=LLM_MAX_TOKENS,
    timeout=None,
    metadata=None,
    structured_json=True,
):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if "deepseek-v4-" in str(model).lower():
        # V4 thinking is enabled by default at effort "high", and on Flash
        # "high" is honoured rather than remapped up. This used to send
        # `disabled` to protect the turn deadline; measured turns then ran 1.9s
        # on 95 completion tokens, and the reasoning was what paid for it.
        # Reasoning turns measured 6.6-24.6s, inside the server's 90s speak
        # budget. Stated explicitly so a self-hosted kit pointed at DeepSeek
        # directly behaves like one behind the arena gateway.
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "high"
    if structured_json and "/api/llm/v1" in str(base):
        # The TEST gateway strips this bounded metadata before forwarding it.
        # It remains on the read-only usage ledger for per-window attribution.
        body["response_format"] = {"type": "json_object"}
        if metadata:
            body["metadata"] = dict(metadata)
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req,
            timeout=max(1, int(timeout or LLM_TIMEOUT_SECONDS)),
        ) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = str(exc)
        lowered = detail.lower()
        overflow_detail = any(marker in lowered for marker in (
            "context overflow",
            "context length exceeded",
            "maximum context length",
            "request_too_large",
            "prompt is too long",
            "input is too long",
            "context_window_exceeded",
            "payload too large",
        ))
        if exc.code == 413 or (exc.code == 400 and overflow_detail):
            raise ContextOverflowError(
                f"provider rejected the conversation context (HTTP {exc.code}): {detail[-600:]}"
            ) from exc
        raise
    choice = data["choices"][0]
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "text": choice["message"]["content"] or "",
        "prompt_tokens": _positive_int(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        ),
        "completion_tokens": _positive_int(
            usage.get("completion_tokens") or usage.get("output_tokens")
        ),
        "reasoning_chars": len(str(choice.get("message", {}).get("reasoning_content") or "")),
        "finish_reason": str(choice.get("finish_reason") or ""),
    }


def _normalize_chat_result(result) -> dict:
    if isinstance(result, dict):
        return {
            "text": str(result.get("text") or ""),
            "prompt_tokens": _positive_int(result.get("prompt_tokens")),
            "completion_tokens": _positive_int(result.get("completion_tokens")),
            "reasoning_chars": _positive_int(result.get("reasoning_chars")),
            "finish_reason": str(result.get("finish_reason") or ""),
        }
    return {
        "text": str(result or ""),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_chars": 0,
        "finish_reason": "",
    }


def _decision_max_tokens(state: dict) -> int:
    if str(state.get("game_type") or "").strip().lower() == "diplomacy":
        return max(1, LLM_DIPLOMACY_MAX_TOKENS)
    return max(1, LLM_MAX_TOKENS)


def _decision_timeout(state: dict) -> int:
    try:
        budget = float(state.get("_decision_budget_seconds") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return max(1, LLM_TIMEOUT_SECONDS)
    return max(1, min(LLM_TIMEOUT_SECONDS, int(budget)))


def _request_metadata(state: dict, stage: str = "inference") -> dict:
    return {
        "clawarena_request_id": str(state.get("_action_window_id") or "")[:80],
        "clawarena_match_id": str(state.get("_match_id") or "")[:32],
        "clawarena_action_window_id": str(state.get("_action_window_id") or "")[:120],
        "clawarena_game_type": str(state.get("game_type") or "")[:40],
        "clawarena_stage": stage[:40],
        "clawarena_client_kind": "clawarena-kit",
        "clawarena_brain": "starter",
    }


_PROJECTION_KEYS = {
    "las_vegas": (
        "round", "total_rounds", "current_roll", "your_roll", "casinos",
        "players", "dice_remaining_by_player", "money_by_player",
    ),
    "mafia": (
        "phase", "day", "my_role", "role", "alive_players", "dead_players",
        "chat_log", "votes", "night_results", "known_roles",
    ),
    "monopoly": (
        "turn", "turn_number", "turn_phase", "current_turn", "my_agent_id",
        "your_agent_id", "position", "your_position", "cash", "your_cash",
        "properties", "your_assets", "opponent_assets", "players",
        "has_rolled_this_turn", "is_your_turn", "is_trade_response_turn",
        "your_space", "pending_property", "pending_trade", "pending_rent",
        "jail", "in_jail", "last_roll", "extra_roll_pending", "last_turn_delta",
        "trade_blocks_this_turn", "trade_openings", "heuristic_advice",
    ),
    "diplomacy": (
        "year", "season", "phase", "power", "my_power", "units", "centers",
        "supply_centers", "powers", "press", "recent_press", "heuristic_advice",
    ),
}

_APPEND_ONLY_PROJECTION_LIMITS = {
    "chat_log": 8,
    "press": 8,
    "recent_press": 8,
}


def _bounded_memory_projection(value: object) -> dict:
    """Keep durable match memory useful while giving it a hard growth bound."""
    if not isinstance(value, dict):
        return {}

    def compact(item, *, depth=0, string_limit=500, list_limit=8):
        if depth >= 4:
            return None
        if isinstance(item, str):
            return item[:string_limit]
        if isinstance(item, list):
            return [
                compact(
                    entry, depth=depth + 1,
                    string_limit=string_limit, list_limit=list_limit,
                )
                for entry in item[-list_limit:]
            ]
        if isinstance(item, dict):
            return {
                str(key)[:80]: compact(
                    entry, depth=depth + 1,
                    string_limit=string_limit, list_limit=list_limit,
                )
                for key, entry in list(item.items())[:24]
            }
        return item

    projected = compact(value)
    if not isinstance(projected, dict):
        return {}
    # The recursive limits above normally stay well below this. If several
    # 500-character notes coincide, retain the newest four entries per list
    # and shorter note excerpts rather than allowing linear prompt growth.
    if len(_ordered_json(projected).encode("utf-8")) > 4096:
        projected = compact(value, string_limit=240, list_limit=4)
    return projected


def _bounded_structured_messages(state: dict, legal_actions: list[dict]) -> list[dict]:
    """Project a live turn to the information needed for one bounded decision.

    Deep reasoning models previously consumed thousands of hidden tokens while
    revisiting an append-only transcript.  The file-backed match memory remains
    authoritative, but each action window now receives a fresh, compact state
    projection so JSON is produced before the gameplay deadline.
    """
    game = str(state.get("game_type") or "").strip().lower()
    projected_state = {
        key: state[key]
        for key in _PROJECTION_KEYS.get(game, ())
        if key in state
    }
    for key, limit in _APPEND_ONLY_PROJECTION_LIMITS.items():
        value = projected_state.get(key)
        if isinstance(value, list) and len(value) > limit:
            projected_state[key] = value[-limit:]
    projected_memory = _bounded_memory_projection(state.get("my_memory"))
    payload = {
        "game_type": game,
        "rules": state.get("game_rules_brief"),
        "strategy": state.get("strategy_brief"),
        "user_preferences": state.get("user_preferences"),
        "message_language": state.get("message_language"),
        "state": projected_state,
        "legal_actions": legal_actions,
        "computed_analysis": _computed_analysis(state, legal_actions),
        "my_memory": projected_memory,
        "action_rejection": state.get("action_rejection"),
    }
    return [
        {"role": "system", "content": BOUNDED_STRUCTURED_PROMPT},
        {"role": "user", "content": _ordered_json(payload)},
    ]


def _chat(base, key, model, state, legal_actions):
    context_window = _model_context_window(base, key, model)
    max_tokens = _decision_max_tokens(state)
    messages, pending = _prepare_conversation(
        state,
        legal_actions,
        context_window=context_window,
        completion_reserve=max_tokens,
    )
    if "deepseek-v4-" in str(model).lower() and "/api/llm/v1" in str(base):
        messages = _bounded_structured_messages(state, legal_actions)
        pending["messages"] = copy.deepcopy(messages)
        pending["mode"] = "bounded_structured"
        pending["estimated_prompt_tokens"] = _estimate_messages_tokens(messages)
    try:
        raw_result = _chat_request(
            base,
            key,
            model,
            messages,
            max_tokens=max_tokens,
            timeout=_decision_timeout(state),
            metadata=_request_metadata(state),
        )
    except ContextOverflowError:
        if pending["mode"] != "delta":
            raise
        messages, pending = _prepare_conversation(
            state,
            legal_actions,
            context_window=context_window,
            completion_reserve=max_tokens,
            force_full=True,
        )
        raw_result = _chat_request(
            base,
            key,
            model,
            messages,
            max_tokens=max_tokens,
            timeout=_decision_timeout(state),
            metadata=_request_metadata(state, "context_recovery"),
        )
    result = _normalize_chat_result(raw_result)
    pending["prompt_tokens"] = result["prompt_tokens"]
    pending["completion_tokens"] = result["completion_tokens"]
    pending["reasoning_chars"] = result["reasoning_chars"]
    pending["finish_reason"] = result["finish_reason"]
    pending["max_completion_tokens"] = max_tokens
    return result["text"], pending


def preflight() -> str:
    """Prove the configured endpoint, key, and model work before queueing."""
    base, key, model = _llm_config()
    if not base or not key or not model:
        raise RuntimeError("LLM_API_KEY or CLAWARENA_GATEWAY_KEY is required")
    raw_result = _chat_request(
        base,
        key,
        model,
        [
            {"role": "system", "content": "Reply with exactly CLAWARENA_READY."},
            {"role": "user", "content": "Connectivity check."},
        ],
        max_tokens=LLM_PREFLIGHT_MAX_TOKENS,
        structured_json=False,
    )
    result = _normalize_chat_result(raw_result)
    reply = result["text"]
    if not reply.strip():
        raise RuntimeError("model returned an empty completion")
    if isinstance(raw_result, dict):
        _model_context_window(base, key, model, discover=True)
    return f"{model} via {base}"


# Chat length limits differ per surface: mafia day/night chat allows 1000
# chars (it IS the game); every other game's table_talk caps at 140.
def _message_cap(action_name: str, state: dict) -> int:
    if action_name == "chat" and state.get("game_type") == "mafia":
        return 1000
    return 140


_COUNTERS = {"llm_calls": 0, "fallbacks": 0}


def _extract_json_object(text):
    """Backward-compatible alias for the kit's shared response parser."""
    return helpers.extract_json_object(text)


def _parse_action(text, legal_actions, state):
    move = _extract_json_object(text)
    if move is None:
        return None
    legal_names = {entry.get("action") for entry in legal_actions}
    if not isinstance(move, dict) or move.get("action") not in legal_names:
        return None
    memo = move.get("memo")
    params = move.get("params")
    move["params"] = params if isinstance(params, dict) else {}
    move, _normalized = _normalize_server_authored_move(move, legal_actions, state)
    if _action_contract_problems(move, legal_actions, state):
        return None
    message = move["params"].get("message")
    if isinstance(message, str):
        cap = _message_cap(move["action"], state)
        if len(message) > cap:
            move["params"]["message"] = message[:cap]
    parsed = {"action": move["action"], "params": move["params"]}
    if isinstance(memo, str) and memo.strip():
        parsed["memo"] = memo.strip()[:200]
    return parsed


def _server_trade_openings(legal_actions: list[dict]) -> list[dict]:
    for entry in legal_actions:
        if not isinstance(entry, dict) or entry.get("action") != "propose_trade":
            continue
        hint = entry.get("hint") if isinstance(entry.get("hint"), dict) else {}
        return helpers.server_trade_openings(hint)
    return []


def _monopoly_non_trade_move(legal_actions: list[dict]) -> dict | None:
    """Choose a parameter-free legal move when no exact trade exists."""
    legal_names = {
        str(entry.get("action") or "")
        for entry in legal_actions
        if isinstance(entry, dict)
    }
    for action in ("roll", "end_turn", "pay_bail", "use_jail_card"):
        if action in legal_names:
            return {"action": action, "params": {}}
    return None


def _server_manage_batch_params(legal_actions: list[dict]) -> dict | None:
    for entry in legal_actions:
        if not isinstance(entry, dict) or entry.get("action") != "manage_batch":
            continue
        hint = entry.get("hint") if isinstance(entry.get("hint"), dict) else {}
        return helpers.server_manage_batch_params(hint)
    return None


def _monopoly_non_batch_move(legal_actions: list[dict]) -> dict | None:
    """Choose a legal phase exit when no current server batch can be bound."""
    legal_names = {
        str(entry.get("action") or "")
        for entry in legal_actions
        if isinstance(entry, dict)
    }
    for action in (
        "roll",
        "end_turn",
        "decline_property",
        "reject_trade_for_turn",
        "reject_trade",
        "pay_bail",
        "use_jail_card",
        "declare_bankruptcy",
    ):
        if action in legal_names:
            return {"action": action, "params": {}}
    return None


def _normalize_server_authored_move(move, legal_actions, state):
    """Keep the model's action choice while binding params to live server hints.

    The match-1179 envelope told the model that a server opening was a starting
    point, while the client validator required byte-for-byte equality.  A harmless
    optional message (or an adjusted cash field) therefore discarded a valid JSON
    reply after a successful provider call.  Choose the closest current server
    opening and preserve only table talk outside its audited economic fields.

    Vegas has the same closed-set boundary at a smaller scale: the model can choose
    ``place`` correctly but name a face that is absent from the current roll.  Keep
    that action choice and deterministically bind the face to the best current
    ``faces_available`` hint.  This is normalization of one provider response, not
    another inference or an untracked heuristic fallback.
    """
    if str(state.get("game_type") or "") == "las_vegas" and move.get("action") == "place":
        entry = next(
            (
                item for item in legal_actions
                if isinstance(item, dict) and item.get("action") == "place"
            ),
            {},
        )
        hint = entry.get("hint") if isinstance(entry.get("hint"), dict) else {}
        faces = [
            item for item in (hint.get("faces_available") or [])
            if isinstance(item, dict) and item.get("face") is not None
        ]
        if faces:
            supplied = move.get("params") if isinstance(move.get("params"), dict) else {}
            supplied_face = supplied.get("face")
            try:
                supplied_face = int(supplied_face)
            except (TypeError, ValueError):
                supplied_face = None
            allowed = {int(item["face"]) for item in faces}
            if supplied_face in allowed:
                if supplied.get("face") != supplied_face:
                    result = dict(move)
                    result["params"] = {**supplied, "face": supplied_face}
                    return result, "las_vegas_face_type"
                return move, ""
            scored = helpers.score_faces(faces)
            if scored and scored[0].get("face") is not None:
                result = dict(move)
                result["params"] = {**supplied, "face": int(scored[0]["face"])}
                return result, "las_vegas_legal_face"
    if str(state.get("game_type") or "") == "monopoly" and move.get("action") == "manage_batch":
        canonical = _server_manage_batch_params(legal_actions)
        if canonical is None:
            safe_move = _monopoly_non_batch_move(legal_actions)
            if safe_move:
                return safe_move, "server_manage_batch_unavailable_nonbatch"
            return move, ""
        supplied = move.get("params") if isinstance(move.get("params"), dict) else {}
        normalized = dict(canonical)
        message = supplied.get("message")
        if isinstance(message, str) and message.strip():
            normalized["message"] = message[:140]
        result = dict(move)
        result["params"] = normalized
        return result, "server_manage_batch"
    if str(state.get("game_type") or "") != "monopoly" or move.get("action") != "propose_trade":
        return move, ""
    openings = _server_trade_openings(legal_actions)
    if not openings:
        safe_move = _monopoly_non_trade_move(legal_actions)
        if safe_move:
            return safe_move, "server_trade_opening_unavailable_nontrade"
        return move, ""
    supplied = move.get("params") if isinstance(move.get("params"), dict) else {}

    def comparable(value):
        if isinstance(value, list):
            return tuple(comparable(item) for item in value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def score(candidate):
        points = 0
        for key, weight in (
            ("to_agent_id", 8),
            ("request_space_ids", 6),
            ("offer_space_ids", 4),
        ):
            if (
                supplied.get(key) not in (None, [], "")
                and comparable(supplied.get(key)) == comparable(candidate.get(key))
            ):
                points += weight
        return points

    selected = max(openings, key=score)
    normalized = dict(selected)
    message = supplied.get("message")
    if isinstance(message, str) and message.strip():
        normalized["message"] = message[:140]
    result = dict(move)
    result["params"] = normalized
    return result, "server_trade_opening"


def _reply_provenance(text, legal_actions, state, *, finish_reason="") -> dict:
    """Return bounded, secret-free evidence for parser/contract outcomes."""
    raw = str(text or "")
    move = _extract_json_object(raw)
    action = move.get("action") if isinstance(move, dict) else ""
    normalized_action = action
    normalized = ""
    problems = []
    if isinstance(move, dict):
        candidate = dict(move)
        candidate["params"] = (
            dict(candidate["params"])
            if isinstance(candidate.get("params"), dict)
            else {}
        )
        candidate, normalized = _normalize_server_authored_move(
            candidate, legal_actions, state,
        )
        normalized_action = candidate.get("action")
        problems = _action_contract_problems(candidate, legal_actions, state)
    legal_names = {
        str(entry.get("action") or "")
        for entry in legal_actions
        if isinstance(entry, dict)
    }
    if not isinstance(move, dict):
        outcome = "no_json_object"
    elif str(action or "") not in legal_names:
        outcome = "non_legal_action"
    elif problems:
        outcome = "contract_invalid"
    else:
        outcome = "accepted"
    return {
        "event": "clawarena_model_reply_provenance",
        "action_window_id": str(state.get("_action_window_id") or "")[:120],
        "brain": "starter",
        "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20],
        "response_chars": len(raw),
        "finish_reason": str(finish_reason or "")[:40],
        "parsed_action": str(action or "")[:60],
        "normalized_action": str(normalized_action or "")[:60],
        "outcome": outcome,
        "normalization": normalized,
        "contract_problems": [
            str(problem).lower().replace(" ", "_")[:100]
            for problem in problems[:5]
        ],
    }


def _action_contract_problems(move, legal_actions, state) -> list[str]:
    """Early, closed-set validation for the high-risk structured params."""
    legal = {
        str(entry.get("action")): entry
        for entry in legal_actions
        if isinstance(entry, dict) and entry.get("action")
    }
    action = str(move.get("action") or "")
    params = move.get("params") if isinstance(move.get("params"), dict) else {}
    hint = legal.get(action, {}).get("hint")
    hint = hint if isinstance(hint, dict) else {}
    problems = []
    game = str(state.get("game_type") or "")
    if game == "las_vegas" and action == "place":
        allowed = {
            entry.get("face") for entry in (hint.get("faces_available") or [])
            if isinstance(entry, dict)
        }
        if params.get("face") not in allowed:
            problems.append("face is not in current faces_available")
    if game == "mafia" and action in {"vote", "night_action"}:
        candidates = hint.get("candidates") or hint.get("targets") or []
        allowed = set()
        for candidate in candidates:
            if isinstance(candidate, dict):
                allowed.add(candidate.get("target_id", candidate.get("agent_id")))
            else:
                allowed.add(candidate)
        if allowed and params.get("target_id") not in allowed:
            problems.append("target_id is not in current candidates")
    if game == "monopoly" and action == "propose_trade":
        openings = _server_trade_openings(legal_actions)
        economic_params = {key: value for key, value in params.items() if key != "message"}
        if economic_params not in openings:
            problems.append("trade is not an exact server_trade_opening")
    if game == "monopoly" and action == "manage_batch":
        canonical = _server_manage_batch_params(legal_actions)
        economic_params = {key: value for key, value in params.items() if key != "message"}
        if canonical is None or economic_params != canonical:
            problems.append("manage_batch is not the current server_manage_batch")
    if game == "monopoly" and action == "accept_trade":
        advice = state.get("heuristic_advice") or {}
        recommendation = advice.get("recommended_action") if isinstance(advice, dict) else None
        recommended_name = (
            recommendation.get("action") if isinstance(recommendation, dict) else recommendation
        )
        if recommended_name != "accept_trade":
            problems.append("server liquidity/monopoly guard did not recommend acceptance")
    return problems


def _note_fallback(reason: str, *, guidance: str | None = None) -> None:
    _COUNTERS["fallbacks"] += 1
    n, f = _COUNTERS["llm_calls"], _COUNTERS["fallbacks"]
    guidance = guidance or "check the key, base URL, model, and response format"
    print(
        f"[llm_agent] WARNING: {reason} — HEURISTIC played this turn, not your LLM "
        f"(fallbacks {f}/{n} calls). Repeated fallbacks mean you are paying for a "
        f"bot that is not using your model — {guidance}.",
        flush=True,
    )


# Diplomacy batches are validated against the CURRENT hints before submitting.
# A hallucinated support pair or coast-less build otherwise seals, adjudicates
# INVALID on the server, and silently wastes the power's whole phase.
_DIPLOMACY_BATCH_ACTIONS = {
    "send_press",
    "submit_orders",
    "submit_retreats",
    "submit_adjustments",
}


def _diplomacy_hint(
    action_name: str,
    legal_actions: list[dict],
    state: dict | None = None,
) -> dict:
    for entry in legal_actions:
        if isinstance(entry, dict) and entry.get("action") == action_name:
            hint = entry.get("hint")
            result = dict(hint) if isinstance(hint, dict) else {}
            advice = (state or {}).get("heuristic_advice")
            if isinstance(advice, dict) and advice:
                result["heuristic_advice"] = advice
            return result
    return {}


def _repair_diplomacy_move(move, reply, pending, base, key, model, state, legal_actions):
    """Deadline-safe hint validation for diplomacy batches (mirrors agent.py).

    A window gets one provider call. If its batch conflicts with the current
    server hints, only the offending orders degrade — movement to HOLD,
    retreat to DISBAND, adjustment to WAIVE, unsalvageable entries dropped —
    and the valid rest still submits without a second inference.
    """
    if state.get("game_type") != "diplomacy" or move["action"] not in _DIPLOMACY_BATCH_ACTIONS:
        return move, reply, pending
    problems = helpers.diplomacy_batch_problems(
        move["action"],
        move["params"],
        _diplomacy_hint(move["action"], legal_actions, state),
    )
    if not problems:
        return move, reply, pending
    safe_params, notes = helpers.degrade_diplomacy_batch(
        move["action"],
        move["params"],
        _diplomacy_hint(move["action"], legal_actions, state),
    )
    if notes:
        print(
            "[llm_agent] diplomacy batch degraded without reinference to stay hint-legal: "
            + "; ".join(notes),
            flush=True,
        )
    move = dict(move)
    move["params"] = safe_params
    return move, reply, pending


def decide(state: dict, legal_actions: list[dict]) -> dict:
    base, key, model = _llm_config()
    if base:
        _COUNTERS["llm_calls"] += 1
        if _COUNTERS["llm_calls"] % 25 == 0:
            print(f"[llm_agent] cost meter: {_COUNTERS['llm_calls']} LLM calls this session "
                  f"({_COUNTERS['fallbacks']} fallbacks)", flush=True)
        started = time.monotonic()
        try:
            reply, pending = _chat(base, key, model, state, legal_actions)
            print(json.dumps(
                _reply_provenance(
                    reply,
                    legal_actions,
                    state,
                    finish_reason=pending.get("finish_reason"),
                ),
                separators=(",", ":"),
            ), flush=True)
            move = _parse_action(reply, legal_actions, state)
            if move:
                move, reply, pending = _repair_diplomacy_move(
                    move, reply, pending, base, key, model, state, legal_actions,
                )
                committed = _commit_conversation(pending, reply)
                if committed and committed[0] != "delta":
                    mode, turn_count = committed
                    prompt_tokens = _positive_int(pending.get("prompt_tokens"))
                    context_window = _positive_int(pending.get("context_window"))
                    budget = (
                        f", prompt {prompt_tokens}/{context_window} tokens"
                        if prompt_tokens and context_window
                        else ""
                    )
                    print(
                        f"[llm_agent] context checkpoint: {mode} baseline "
                        f"(match turn {turn_count}{budget})",
                        flush=True,
                    )
                print(json.dumps({
                    "event": "clawarena_decision",
                    "action_window_id": str(state.get("_action_window_id") or ""),
                    "stage": "decision_ready",
                    "brain": "starter",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "fallback_reason": "",
                }, separators=(",", ":")), flush=True)
                return move
            if str(pending.get("finish_reason") or "").lower() == "length":
                _note_fallback(
                    "LLM completion hit the configured "
                    f"{pending.get('max_completion_tokens')} token limit before producing valid JSON",
                    guidance=(
                        "raise LLM_MAX_TOKENS (or LLM_DIPLOMACY_MAX_TOKENS) "
                        "for this reasoning model"
                    ),
                )
            else:
                _note_fallback("unusable LLM reply")
        except Exception as exc:  # noqa: BLE001 — never lose the turn to the model
            _note_fallback(f"LLM call failed ({exc})")
        print(json.dumps({
            "event": "clawarena_decision",
            "action_window_id": str(state.get("_action_window_id") or ""),
            "stage": "decision_ready",
            "brain": "starter",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "fallback_reason": "model_unavailable_or_invalid",
        }, separators=(",", ":")), flush=True)
    try:
        move = heuristic_agent.decide(state, legal_actions)
        move, normalized = _normalize_server_authored_move(
            move, legal_actions, state,
        )
        if normalized:
            print(json.dumps({
                "event": "clawarena_deterministic_normalization",
                "action_window_id": str(state.get("_action_window_id") or ""),
                "brain": "starter",
                "normalization": normalized,
                "action": str(move.get("action") or ""),
            }, separators=(",", ":")), flush=True)
        return move
    except Exception as exc:  # noqa: BLE001 — the turn must never be lost to a bug
        print(f"[llm_agent] heuristic crashed ({exc}) — playing first legal action", flush=True)
        first = legal_actions[0]
        return {"action": first.get("action"), "params": {}}


def report(state: dict, move: dict) -> None:
    """Per-turn owner report, delivered only when a destination is configured.

    The runner calls this on exactly the turns the dashboard's Report Level and
    the server's ``report_important`` flag allow, and only when the brain exposes
    it — which is why the default kit was silent before this existed. Delegates
    to report_sink so a self-hosted user can swap the destination (or import the
    sink from their own brain) without touching decision code.
    """
    report_sink.report(state, move)
