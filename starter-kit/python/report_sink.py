"""Send per-turn match reports to the owner's own Telegram bot.

The runner already decides WHEN to report — it mirrors the dashboard's Report
Level and the server's per-turn ``report_important`` flag exactly (see
``runner._should_deliver``). What the default Starter Kit never had was a WHERE,
so the setting controlled nothing. This module is that missing half.

Configuration is two environment variables, and their absence is the off switch:

  CLAWARENA_REPORT_TELEGRAM_TOKEN    bot token from @BotFather
  CLAWARENA_REPORT_TELEGRAM_CHAT_ID  chat to post into

Team-hosted agents get both injected from the channel their owner connected in
Command Center. Self-hosted runners can export them by hand — same behaviour,
same message.

Delivery rules, all of them chosen so a chat problem can never become a gameplay
problem: one in-flight send at a time, a hard timeout, and every failure
swallowed after a single stderr line. A missed report costs a notification; a
blocked poll loop costs the match.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV = "CLAWARENA_REPORT_TELEGRAM_TOKEN"
CHAT_ENV = "CLAWARENA_REPORT_TELEGRAM_CHAT_ID"
TIMEOUT_SECONDS = float(os.environ.get("CLAWARENA_REPORT_TIMEOUT_SECONDS", "10") or 10)
MAX_MESSAGE_CHARS = 900

_sending = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip() and os.environ.get(CHAT_ENV, "").strip())


def _params(state: dict, move: dict) -> str:
    params = move.get("params")
    if not isinstance(params, dict) or not params:
        return ""
    try:
        return " " + json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def build_message(state: dict, move: dict) -> str:
    """One line of what happened, plus the agent's own one-line memo if it wrote
    one. The memo is the whole reason a client-side sink beats a server-side
    one: only the runner knows why it played that."""
    game = str(state.get("game_type") or "ClawArena").replace("_", " ").title()
    action = str(move.get("action") or "move")
    phase = str(state.get("phase") or "").strip()
    match_id = state.get("match_id") or state.get("match") or ""
    head = f"[ClawArena · {game}]"
    if match_id:
        head += f" match {match_id}"
    line = f"{head}\nPlayed {action}{_params(state, move)}"
    if phase:
        line += f" during {phase}"
    memo = str(move.get("memo") or "").strip()
    if memo:
        line += f"\n> {memo}"
    return line[:MAX_MESSAGE_CHARS]


def _post(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
        payload = json.loads(res.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description") or "telegram rejected the message"))


def report(state: dict, move: dict) -> None:
    """Queue one report. Never raises, never blocks the caller."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    chat_id = os.environ.get(CHAT_ENV, "").strip()
    if not token or not chat_id:
        return
    text = build_message(state or {}, move or {})

    def deliver() -> None:
        # Single-flight: a stuck send must not pile up one thread per turn.
        if not _sending.acquire(blocking=False):
            print("[report] skipped (previous delivery still running)", file=sys.stderr, flush=True)
            return
        try:
            _post(token, chat_id, text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            print(f"[report] telegram HTTP {exc.code}: {detail}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 — a report must never break play
            print(f"[report] failed ({exc})", file=sys.stderr, flush=True)
        finally:
            _sending.release()

    try:
        threading.Thread(target=deliver, name="clawarena-report", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 — thread exhaustion is not a game over
        print(f"[report] failed to start ({exc})", file=sys.stderr, flush=True)
