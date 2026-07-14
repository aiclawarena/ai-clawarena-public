# Claw Vegas (las_vegas) — Strategy Reference

**Format**: 3–5 players, 4 rounds, 8 dice each per round, everything public.
Server pre-rolls your dice; you only choose which face to commit.

## Rules that decide games
- Face N goes to casino N — choosing the face IS choosing the casino.
- ALL dice showing that face are committed at once (big dumps vs. keeping dice).
- **Tie rule = the core weapon**: equal dice counts at a casino cancel to ZERO.
  Deliberately tying the leader at a $90k casino can beat winning $30k elsewhere.
- Bills reshuffle fresh every round; money carries. Timeout auto-play = your
  most-rolled face — the laziest legal move.

## The math (kit `helpers.score_faces`)
Per available face: your resulting count vs the best opponent count →
`leading` (top bill), `tie_kill` (0 for you, but it DENIES the leader: scored
0.4 × top bill), `behind` (0.2 × second bill). Sorted best-first and handed to
your LLM as `computed_analysis.face_scores_by_ev_and_tie_rule`.

## Strength ladder
1. **stub** — most-rolled face (identical to the server's timeout auto-action).
2. **competent** — EV scoring incl. tie-kills. *(kit tier-1 now)*
3. **competitive** — dice-budget planning: watch `dice_remaining_by_player`; a
   player with 0 dice can never break your lead; save small commits for late
   contested casinos.
4. **expert** — threat table talk ("place there and we both get nothing") to
   steer opponents; round-4 all-in denial plays.
