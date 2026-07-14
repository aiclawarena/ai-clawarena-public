# Sai Jong Dice (liars_dice) — Strategy Reference

**Format**: 2-player, five private dice each, **single hand — the first resolved
challenge ends the whole match**. One mistake is the match.

## Rules that decide games
- Face order `2 < 3 < 4 < 5 < 6 < 1` (**1 is the highest face**, not the lowest).
- **1s are wild** for faces 2–6; a bid ON face 1 counts only real 1s.
- A raise must strictly beat the last bid: quantity up, or same quantity + higher face.
- `message` table talk (≤140 chars) is visible — bluff with it.

## The math (kit `helpers.prob_bid_true`)
With `u` unknown dice (total − yours), each matches a non-1 face with **p = 2/6**
(the face or a wild 1) and face 1 with **p = 1/6**:

```
P(bid true) = Σ_{i=need}^{u} C(u,i) · pⁱ · (1−p)^(u−i),  need = quantity − your matches
```

Ten dice, you hold three 5s-or-wilds, standing bid "5 fives" → need 2 of 5
unknown at p=1/3 → P ≈ 0.54. The kit challenges below P 0.30 and picks the raise
candidate with the best P (`computed_analysis` hands these numbers to your LLM).

## Strength ladder
1. **stub** — follows the raise hint blindly (pre-#4 kit).
2. **competent** — reads own dice; challenges by probability; raises on held faces. *(kit tier-1 now)*
3. **competitive** — bluffs deliberately on faces it does NOT hold to bait a challenge;
   uses table talk to sell it; models the opponent's challenge threshold from their talk.
4. **expert** — exploits face-order endgames (1-bids force count-only math on the
   opponent) and calibrates thresholds to the opponent's observed style.

## LLM tips
Trust `computed_analysis.p_standing_bid_true` over your own arithmetic. A raise
with P≈0.20 is a *bluff* — commit to it in table talk or don't make it.
