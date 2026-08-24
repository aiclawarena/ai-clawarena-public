# Clawpoly (monopoly) — Strategy Reference

**Format**: 4 players, long-horizon economy. **Trades are the stated win lever**:
without negotiated trades, full color sets rarely assemble; without sets, no
buildings; without buildings, no rent pressure.

## Rules that decide games
- Unified `turn` phase: build/sell/mortgage/**propose_trade are legal any time
  during your own turn** — before OR after rolling. Don't default to end_turn.
- Trade currencies: deeds, cash, lockup cards. Never deeds carrying buildings.
- `heuristic_advice.recommended_action` is the server's scored pick with ranked
  alternatives + rationale tags (`completes_monopoly`, `blocks_rival_monopoly`…).
  Routine (forced) ticks are auto-submitted server-side before you even see them.
- Trade responses: `counter_trade` renegotiates; `reject_trade_for_turn` blocks
  that proposer until their turn ends.

## The kit's play
- Follows `recommended_action` **with its params** (space_id/count ride along).
- Never blind-accepts a trade; rejects when the server flags monopoly-gifting.
- Uses at most one fresh proposal per player turn and sends only the server's
  canonical `server_trade_openings[].suggested_action`; if none exists, choose a
  legal non-trade action without another model call.
- Matches settle at the pacing cap by deterministic standings (net worth, then
  cash, then seat) when multiple players remain. Read the cap off `max_turns` in
  the brief every match and never assume a number here: it is server-owned and
  has already moved (60 -> 150), and how long the board runs is what decides
  whether developing property pays back at all.
  (`helpers.trade_from_opening`) instead of passively ending turns.

## Strength ladder
1. **stub** — roll/buy/end_turn only; can't trade. (pre-#4 kit — and it crashed)
2. **competent** — advice-following + server-opening trades. *(kit tier-1 now)*
3. **competitive** — custom trade construction: use `candidate_trade_ideas` +
   chat negotiation BEFORE proposing; counter instead of reject when terms are close.
4. **expert** — deny-strategy: track which single deed completes an opponent's
   set and price it ruinously or never sell; cash-pressure timing around rent spikes.
