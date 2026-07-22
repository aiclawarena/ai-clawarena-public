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
import json
import os
import threading
import urllib.error
import urllib.request

import agent as heuristic_agent
import helpers
import memory
from arena_client import base_url

LLM_TIMEOUT_SECONDS = 45
# Reasoning models (e.g. deepseek-v4-flash) spend tokens on hidden reasoning
# BEFORE the visible reply — a tight cap returns empty/truncated content and
# the turn falls back to the heuristic (a pinned completion_tokens == cap in
# your usage log is the tell; live matches still pinned 1200 occasionally).
# Models stop when done, so headroom only costs on the turns that need it.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "3000"))
LLM_DIPLOMACY_MAX_TOKENS = int(
    os.environ.get("LLM_DIPLOMACY_MAX_TOKENS", "6000")
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
ids elsewhere in state are a different id space. Long, specific, persuasive chat wins.
- las_vegas: payouts settle per casino and TIED dice counts CANCEL to zero — placing \
onto a tie can neutralize a leader or waste your dice; read casino totals and everyone's \
remaining dice before committing.
- monopoly: state.heuristic_advice.recommended_action is scored server advice — follow \
it unless you have a concrete reason not to; build trade params from the advice/hints, \
and never accept a trade that completes an opponent's color set.
- diplomacy: agreements are non-binding. heuristic_advice is tactical decision support, \
not a command: compare its top candidates against visible press and your own strategy. In \
press rounds, contact only position-relevant powers with concrete proposals and stay silent \
when there is no useful ask or reply. In order phases, choose candidate_id, patch at most two \
origins, or submit a fully custom atomic batch. Never expose private press or pending orders.

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


def _chat_request(base, key, model, messages, *, max_tokens=LLM_MAX_TOKENS):
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
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
        "finish_reason": str(choice.get("finish_reason") or ""),
    }


def _normalize_chat_result(result) -> dict:
    if isinstance(result, dict):
        return {
            "text": str(result.get("text") or ""),
            "prompt_tokens": _positive_int(result.get("prompt_tokens")),
            "finish_reason": str(result.get("finish_reason") or ""),
        }
    return {"text": str(result or ""), "prompt_tokens": 0, "finish_reason": ""}


def _decision_max_tokens(state: dict) -> int:
    if str(state.get("game_type") or "").strip().lower() == "diplomacy":
        return max(1, LLM_DIPLOMACY_MAX_TOKENS)
    return max(1, LLM_MAX_TOKENS)


def _chat(base, key, model, state, legal_actions):
    context_window = _model_context_window(base, key, model)
    max_tokens = _decision_max_tokens(state)
    messages, pending = _prepare_conversation(
        state,
        legal_actions,
        context_window=context_window,
        completion_reserve=max_tokens,
    )
    try:
        raw_result = _chat_request(
            base,
            key,
            model,
            messages,
            max_tokens=max_tokens,
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
        )
    result = _normalize_chat_result(raw_result)
    pending["prompt_tokens"] = result["prompt_tokens"]
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
    message = move["params"].get("message")
    if isinstance(message, str):
        cap = _message_cap(move["action"], state)
        if len(message) > cap:
            move["params"]["message"] = message[:cap]
    parsed = {"action": move["action"], "params": move["params"]}
    if isinstance(memo, str) and memo.strip():
        parsed["memo"] = memo.strip()[:200]
    return parsed


def _note_fallback(reason: str) -> None:
    _COUNTERS["fallbacks"] += 1
    n, f = _COUNTERS["llm_calls"], _COUNTERS["fallbacks"]
    print(
        f"[llm_agent] WARNING: {reason} — HEURISTIC played this turn, not your LLM "
        f"(fallbacks {f}/{n} calls). Repeated fallbacks mean you are paying for a "
        "bot that is not using your model — check the key/base URL.",
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

    An invalid batch earns ONE corrective LLM retry carrying the validator's
    findings. If the retry is still invalid (or fails), only the offending
    orders degrade — movement to HOLD, retreat to DISBAND, adjustment to
    WAIVE, unsalvageable entries dropped — and the valid rest still submits.
    Returns the (possibly replaced) move, reply, and pending transcript.
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
    try:
        _COUNTERS["llm_calls"] += 1
        retry_messages = copy.deepcopy(pending["messages"])
        retry_messages.append({"role": "assistant", "content": reply})
        retry_messages.append({"role": "user", "content": (
            "ORDER_VALIDATION_FAILED: your reply is not legal for the current "
            "phase hints and the server would waste those orders: "
            + json.dumps(problems[:5], ensure_ascii=False)
            + "\nReply again with ONLY the corrected JSON action. Use origins, "
            "destinations, support pairs, and build sites EXACTLY as listed in "
            "legal_actions[].hint (legal_orders + shared_candidates)."
        )})
        result = _normalize_chat_result(_chat_request(
            base,
            key,
            model,
            retry_messages,
            max_tokens=_decision_max_tokens(state),
        ))
        retried = _parse_action(result["text"], legal_actions, state)
        if retried and retried["action"] in _DIPLOMACY_BATCH_ACTIONS:
            retry_pending = dict(pending)
            retry_pending["messages"] = retry_messages
            retry_pending["prompt_tokens"] = result["prompt_tokens"]
            retry_problems = helpers.diplomacy_batch_problems(
                retried["action"],
                retried["params"],
                _diplomacy_hint(retried["action"], legal_actions, state),
            )
            if not retry_problems:
                print("[llm_agent] diplomacy retry produced a hint-legal batch", flush=True)
                return retried, result["text"], retry_pending
            # Degrade the retried batch: it is the model's latest intent.
            move, reply, pending = retried, result["text"], retry_pending
    except Exception as exc:  # noqa: BLE001 — degrade the last parsed batch instead
        print(f"[llm_agent] diplomacy validation retry failed ({exc})", flush=True)
    safe_params, notes = helpers.degrade_diplomacy_batch(
        move["action"],
        move["params"],
        _diplomacy_hint(move["action"], legal_actions, state),
    )
    if notes:
        print(
            "[llm_agent] diplomacy batch degraded to stay hint-legal: "
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
        try:
            reply, pending = _chat(base, key, model, state, legal_actions)
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
                return move
            if str(pending.get("finish_reason") or "").lower() == "length":
                _note_fallback(
                    "LLM completion hit the configured "
                    f"{pending.get('max_completion_tokens')} token limit before producing valid JSON"
                )
            else:
                _note_fallback("unusable LLM reply")
        except Exception as exc:  # noqa: BLE001 — never lose the turn to the model
            _note_fallback(f"LLM call failed ({exc})")
    try:
        return heuristic_agent.decide(state, legal_actions)
    except Exception as exc:  # noqa: BLE001 — the turn must never be lost to a bug
        print(f"[llm_agent] heuristic crashed ({exc}) — playing first legal action", flush=True)
        first = legal_actions[0]
        return {"action": first.get("action"), "params": {}}
