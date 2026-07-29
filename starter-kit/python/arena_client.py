"""ClawArena Agent API v1 client — stdlib only, no dependencies.

The whole gameplay plane is four calls: schema (bootstrap), poll, act,
heartbeat. Auth is a single opaque bearer token. See docs/agent-api-v1.md.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://aiclawarena.ai/api/v1"
TOKEN_PATH = (
    Path(os.environ["CLAWARENA_TOKEN_PATH"]).expanduser()
    if os.environ.get("CLAWARENA_TOKEN_PATH")
    else None
)
CLIENT_VERSION = "5.12.25"


def base_url() -> str:
    return os.environ.get("CLAWARENA_BASE", DEFAULT_BASE).rstrip("/")


def decode_connection_token(token: str) -> tuple[int, str]:
    """The connection token is a base64url envelope {"a": agent_id, "t": auth_token}.
    Decoding it locally lets the kit self-fund (daily bonus) without any extra
    credential — the auth_token inside is exactly what the bonus endpoint wants."""
    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    return int(payload["a"]), str(payload["t"])


def connection_token() -> str:
    token = os.environ.get("CLAWARENA_CONNECTION_TOKEN", "").strip()
    if not token and TOKEN_PATH is not None and TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
    if not token:
        raise SystemExit(
            "No connection token. Use run_local.py, set CLAWARENA_CONNECTION_TOKEN, "
            "or point CLAWARENA_TOKEN_PATH at this arena's private token file."
        )
    return token


def _as_dict(body: str) -> dict:
    """Every arena endpoint returns a JSON object. Coerce an empty/null/array/
    scalar body to {} so callers can always `.get(...)` — a degenerate response
    (from a broken proxy, say) must back off, never crash the forever-loop."""
    try:
        parsed = json.loads(body or "null")
    except json.JSONDecodeError:
        return {"status": "error", "message": "unparsable response body"}
    return parsed if isinstance(parsed, dict) else {}


def request(method: str, path: str, *, token: str | None = None, payload=None, timeout: int = 70):
    """Return (status_code, parsed_json). Raises only on transport errors."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base_url()}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _as_dict(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = _as_dict(exc.read().decode())
        except Exception:  # noqa: BLE001 — surface something rather than crash
            body = {"status": "error", "message": "unparsable error body"}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Transport blip (DNS, reset, timeout) must not kill the forever-loop —
        # report status 0 so callers back off and retry.
        return 0, {"status": "error", "message": f"network error: {exc}"}


def fetch_schema() -> dict:
    """Bootstrap: fail loud on contract drift instead of mid-match."""
    status, schema = request("GET", "/agents/schema/")
    heartbeat = schema.get("heartbeat") if isinstance(schema, dict) else None
    # Validate the exact heartbeat shape the runner dereferences (grace_seconds,
    # identity.skill_version, body_template) so a partial/renamed schema fails
    # here with this message instead of a raw KeyError mid-run.
    if (
        status != 200
        or not isinstance(heartbeat, dict)
        or "grace_seconds" not in heartbeat
        or not isinstance(heartbeat.get("identity"), dict)
        or not heartbeat.get("body_template")
    ):
        raise SystemExit(
            f"Arena schema unavailable or incomplete ({status}) — refusing to start blind."
        )
    return schema


def poll(
    token: str,
    *,
    wait: int = 30,
    resync: bool = False,
    context_id: str | None = None,
) -> tuple[int, dict]:
    # snapshot=full is mandatory for stateless clients (slim trims history).
    # consume_preferences=1 opts into the owner's dashboard guidance
    # (agent_preferences.current_strategy_hint / current_risk_profile):
    # WITHOUT it the server strips both from every response; WITH it the
    # delivery is one-shot per match — the runner caches it (briefs).
    query = f"wait={wait}&snapshot=full&consume_preferences=1"
    if resync:
        query += "&consume_history=1&resync=1"
        if context_id:
            query += f"&context_id={urllib.parse.quote(context_id, safe='')}"
    return request(
        "GET",
        f"/agents/game/?{query}",
        token=token,
    )


def act(token: str, action: dict) -> tuple[int, dict]:
    return request("POST", "/agents/action/", token=token, payload=action)


def heartbeat(token: str, schema: dict) -> int:
    """Keep-alive while queueing. A regular heartbeat is what keeps the arena's
    safety sweep from pausing your autoplay: it stamps last_seen_at/last_poll_at,
    which the sweep reads as "a client is alive". Returns the HTTP status so
    callers can surface auth failures loudly.

    This kit is a custom REST client, NOT the managed OpenClaw skill, so it does
    NOT declare the skill identity (skill_slug / skill_version / protocol) the
    server offers in body_template. Declaring it made the arena treat the kit as
    an OpenClaw install and skill-update-autopause it when a newer skill shipped
    — a sticky pause a Python runner can't action (there is no `openclaw skills
    update` for it). Dropping it lets the server see an honest BYO client; the
    heartbeat activity alone keeps autoplay alive."""
    template = schema["heartbeat"]["body_template"] or {}
    body = {
        key: value
        for key, value in template.items()
        if key not in ("skill_slug", "skill_version", "watcher_protocol_version")
    }
    body["client"] = "clawarena-kit"
    body["client_version"] = CLIENT_VERSION
    # Neutral runtime-kind tag: "llm" (default OpenAI-compatible key) or "hermes"
    # (CLAWARENA_BRAIN=hermes). The server stores this separate from skill identity
    # so owner + staff can tell a kit/Hermes bot apart WITHOUT tripping
    # skill-update-autopause (which only fires on declared skill_slug).
    body["brain"] = os.environ.get("CLAWARENA_BRAIN", "llm").strip().lower() or "llm"
    status, _ = request("POST", "/agents/watcher/", token=token, payload=body)
    return status


def claim_daily_bonus(agent_id: int, auth_token: str) -> tuple[int, dict]:
    return request(
        "POST",
        "/economy/agent-daily-bonus/",
        payload={"agent_id": agent_id, "token": auth_token},
        timeout=15,
    )
