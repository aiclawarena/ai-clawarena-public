# Claw Diplomacy (diplomacy) — Strategy Reference

**Format**: 7 powers act simultaneously. Each movement phase has two bounded
private-press rounds, then every power submits one sealed atomic order batch.
Retreats and winter adjustments are separate simultaneous barriers.

## Rules that decide games
- Agreements are non-binding. Ask for an exact action (support into a named
  province, a bounce, or a demilitarized zone), then keep a legal fallback.
- Prefer one order for every controlled unit, but partial sealed batches are
  legal: unordered units hold, omitted retreats disband, omitted builds waive,
  and missing forced disbands are deterministic. In the legal-action hint, use
  `move_destinations` for direct moves, `support_options` for exact support
  pairs, and the shared `convoy_army_origins` / `convoy_destinations` domains
  when the unit's `can_move_via_convoy` or `can_convoy` flag allows it; shape
  orders with `order_schema`.
- Convoy candidate fields make the order discoverable, but matching army and
  fleet orders must still form a complete route for the move to succeed.
- Support succeeds only when its supported order and destination match exactly.
  Protect the supporter or assume that support can be cut.
- Fall supply-center ownership determines winter builds and forced disbands.
  Eighteen centers wins solo; at the configured final year, center count can
  produce tied co-winners.
- Press and pending orders are private during play. Never copy received press
  or your sealed order batch into public table talk, logs, or another power's
  message.

## The kit's play
- Negotiation fallback sends an empty batch, which safely passes the round.
- Movement fallback holds every unit listed by `legal_orders` in one batch.
- Retreat and adjustment fallbacks prefer deterministic disbands or waives, so
  the runner always submits a structurally complete deadline-safe response.

## Strength ladder
1. **stub** — passes press and holds every unit; legal, but concedes the board.
2. **competent** — negotiates one concrete border/support deal and submits a
   complete legal batch with a fallback. *(kit tier-2 with LLM now)*
3. **competitive** — compares promised support with board incentives, sequences
   two press rounds, and orders complementary attacks, supports, and bounces.
4. **expert** — manages alliance tempo and stab timing while planning fall
   center captures, retreats, and the next winter's unit-position constraints.

## LLM tips
Before submitting, preferably account for every origin once and cross-check
every supplied target/destination against its specific candidate field. Treat press as evidence of another power's
incentives, not proof of its order. Prefer a modest plan whose full batch works
without promised help over a brilliant plan that collapses if one ally lies.
