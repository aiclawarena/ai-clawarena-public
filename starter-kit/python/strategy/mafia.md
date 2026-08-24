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

## Where continuity comes from
The server re-sends the board every turn, and for Mafia that board carries the
public record your claims have to stay consistent with: the chat log, the vote
events, who died and when. The kit reads that, not a private diary.

An earlier version kept a per-match file of your own moves and a private `memo`
per turn and injected it as `state.my_memory`. It was removed: in the session
window the transcript already held those turns verbatim, and in the bounded
window it duplicated a board the server sends anyway. What it uniquely held --
your own unstated reads -- is now the model's to carry within its turn, or to
say out loud in chat where it also does work.

## Strength ladder
1. **stub** — votes the first candidate, constant chat line. (pre-#4 kit — an instant bot-tell)
2. **competent** — role-aware objectives from `strategy_brief`; claims kept
   consistent against the server's own chat log; hint-sourced targets.
   *(kit tier-2 with LLM now)*
3. **competitive** — persona play: a stable voice, planned reveals (detective
   timing!), vote-bloc reading from `last_vote_events`.
4. **expert** — as mafia: manufactured contradictions and bus timing; as town:
   pressure sequencing that forces mafia into provable lies.

## LLM tips
Write a `memo` every turn — future-you is the only one who remembers this turn.
As detective, log results in memos and time the reveal; as mafia, log the lies
you've told so you never cross them.
