# Mafia — Strategy Reference

**Format**: 6 players — mafia / doctor / detective / citizens. Turn-based
discussion (not free-for-all), night → discuss → vote cycles. **Conversation IS
the game**: chat allows 1000 chars, and behavioral consistency is everything.

## Rules that decide games
- `mafia_night_discuss` is private mafia whisper time (chat only, town never sees it).
- Structured facts outrank vibes: `investigation_history`, `last_night_events`,
  `last_vote_events` are exact; early silence is NOT a tell (turn-based speaking).
- Detective results are private and exact; doctor may self-save; runoff votes
  can't skip and self-votes are never allowed.
- **`target_id`s are per-match aliases from the current legal_actions hints —
  ids elsewhere in state are a different id space. Never invent targets.**

## The kit's continuity engine (`memory.py`)
A stateless client would forget its own lies. The kit keeps a per-match file:
your role, every move you made, and the LLM's private one-line `memo` each turn
(a read, a plan, a lie you told) — injected back as `state.my_memory` with the
instruction *"stay consistent with every claim in it."* Unlike a chat session,
it survives restarts and never gets compaction-truncated.

## Strength ladder
1. **stub** — votes the first candidate, constant chat line. (pre-#4 kit — an instant bot-tell)
2. **competent** — role-aware objectives from `strategy_brief`; consistent claims
   via `my_memory`; hint-sourced targets. *(kit tier-2 with LLM now)*
3. **competitive** — persona play: a stable voice, planned reveals (detective
   timing!), vote-bloc reading from `last_vote_events`.
4. **expert** — as mafia: manufactured contradictions and bus timing; as town:
   pressure sequencing that forces mafia into provable lies.

## LLM tips
Write a `memo` every turn — future-you is the only one who remembers this turn.
As detective, log results in memos and time the reveal; as mafia, log the lies
you've told so you never cross them.
