"""HaloArena Agent API v1 client — stdlib only, no dependencies.

The whole gameplay plane is four calls: schema (bootstrap), poll, act,
heartbeat. Auth is a single opaque bearer token. See docs/agent-api-v1.md.
"""
from __future__ import annotations

import base64
import http.client
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
CLIENT_VERSION = "5.13.79"


class _NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The Agent API has no redirect contract. Preserve the original HTTP
        # error/status without forwarding either authorization or secret bodies.
        return None


_API_OPENER = urllib.request.build_opener(_NoAPIRedirect())


def api_urlopen(req, *, timeout):
    return _API_OPENER.open(req, timeout=timeout)


def validated_api_base(api_base: str) -> str:
    value = str(api_base or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(value)
        valid = (
            bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and not parsed.params
            and not any(character.isspace() or ord(character) < 32 for character in value)
            and "\\" not in value
            and (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            ))
        )
        parsed.port  # Reject malformed/out-of-range ports before reading credentials.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("HaloArena API base must be an HTTPS URL without userinfo, query, or fragment (HTTP is allowed only on loopback).")
    return value


def base_url() -> str:
    return validated_api_base(os.environ.get("CLAWARENA_BASE") or DEFAULT_BASE)


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
    """Reject invalid envelopes instead of turning them into successful ACKs."""
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def request(method: str, path: str, *, token: str | None = None, payload=None, timeout: int = 70):
    """Return (status_code, parsed_json); status 0 means an uncertain response."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base_url()}{path}", data=data, headers=headers, method=method)
    try:
        with api_urlopen(req, timeout=timeout) as resp:
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
    except (http.client.HTTPException, UnicodeError, ValueError):
        # The server may already have applied a POST. Keep the same idempotency
        # key when retrying, and never include partial response bytes in logs.
        return 0, {"status": "error", "message": "incomplete or invalid response body"}


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


def delta_transport_enabled() -> bool:
    """Opt in to receiving the board as a diff instead of whole every turn.

    Off by default. It changes what the wire carries, not what anything above
    the transport sees: ``kit.match_state`` folds each delta back into a
    complete board before the response leaves this layer.
    """

    return str(
        os.environ.get("CLAWARENA_DELTA_TRANSPORT", "")
    ).strip().lower() in {"1", "true", "yes", "on"}


def poll(
    token: str,
    *,
    wait: int = 30,
    resync: bool = False,
    context_id: str | None = None,
    state_ack: int | None = None,
    bootstrap: bool = False,
) -> tuple[int, dict]:
    # consume_preferences=1 opts into the owner's dashboard guidance
    # (agent_preferences.current_strategy_hint / current_risk_profile):
    # WITHOUT it the server strips both from every response; WITH it the
    # delivery is one-shot per match — the runner caches it (briefs).
    #
    # snapshot=full is mandatory for a stateless client, because slim trims
    # history it has no way to recover. Under the delta transport the client
    # keeps the board itself, so the legacy top-level state may be slim — asking
    # for it in full there would ship the whole board every turn anyway and
    # defeat the point.
    delta = delta_transport_enabled()
    snapshot = "slim" if delta and not resync else "full"
    # bootstrap primes the server's cursor and always answers whole, which is
    # what a client with no baseline needs.
    profile = "stateless"
    if delta:
        profile = "bootstrap" if (bootstrap or resync) else "session"
    query = (
        f"wait={wait}&snapshot={snapshot}&consume_preferences=1"
        f"&decision_context_version=2&decision_context_profile={profile}"
    )
    if state_ack:
        query += f"&state_ack={int(state_ack)}"
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
    status, body = request("POST", "/agents/action/", token=token, payload=action)
    if status == 200 and body.get("status") != "ok":
        return 0, {"status": "error", "message": "invalid action acknowledgement"}
    return status, body


def _heartbeat_body(
    schema: dict, *, restart_ack: bool = False, restart_generation: str = "",
    restart_completed_generation: str = "", restart_claim_until: str = "",
) -> dict:
    """Build the keep-alive identity used while queueing.

    A regular heartbeat is what keeps the arena's
    safety sweep from pausing your autoplay: it stamps last_seen_at/last_poll_at,
    which the sweep reads as "a client is alive".

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
    if restart_ack:
        body["restart_ack"] = True
        body["restart_generation"] = restart_generation
    if restart_completed_generation:
        body["restart_completed_generation"] = restart_completed_generation
        body["restart_claim_until"] = restart_claim_until
    return body


def heartbeat_with_response(
    token: str,
    schema: dict,
    *,
    restart_ack: bool = False,
    restart_generation: str = "",
    restart_completed_generation: str = "",
    restart_claim_until: str = "",
) -> tuple[int, dict]:
    """Send identity; retry a startup completion on later normal heartbeats."""
    saved = os.environ.get("CLAWARENA_RESTART_COMPLETION", "")
    if saved and not restart_ack and not restart_completed_generation:
        try:
            claim = json.loads(saved)
            restart_completed_generation = claim["generation"]
            restart_claim_until = claim["claim_until"]
        except (ValueError, KeyError, TypeError):
            os.environ.pop("CLAWARENA_RESTART_COMPLETION", None)
    result = request(
        "POST",
        "/agents/watcher/",
        token=token,
        payload=_heartbeat_body(
            schema, restart_ack=restart_ack, restart_generation=restart_generation,
            restart_completed_generation=restart_completed_generation,
            restart_claim_until=restart_claim_until,
        ),
    )
    if restart_completed_generation:
        receipt = result[1].get("restart_completed") if isinstance(result[1], dict) else None
        if result[0] in {401, 403, 409} or (
            result[0] == 200 and receipt == {
                "generation": restart_completed_generation, "claim_until": restart_claim_until,
            }
        ):
            os.environ.pop("CLAWARENA_RESTART_COMPLETION", None)
    return result


def heartbeat(token: str, schema: dict) -> int:
    """Compatibility wrapper for callers that only need the status code."""
    status, _ = heartbeat_with_response(token, schema)
    return status


def claim_daily_bonus(agent_id: int, auth_token: str) -> tuple[int, dict]:
    return request(
        "POST",
        "/economy/agent-daily-bonus/",
        payload={"agent_id": agent_id, "token": auth_token},
        timeout=15,
    )
