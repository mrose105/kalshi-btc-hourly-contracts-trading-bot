# Where things stand — 2026-08-29

A living handoff. Overwrite it; don't append. It exists because context gets
lost — sessions end, get compacted, or resume days later with no memory. On
2026-08-29 four days of work had to be reconstructed from `git log`, which
worked only because the commit messages carried the reasoning. This file makes
that recovery deliberate rather than lucky.

**Read `git log` for the why. Read this for the where.**

---

## What the strategy is

Short-premium selling in binary event contracts. You cannot short a Kalshi
contract, so the position is taken by **buying NO** on an out-of-the-money
band: you are paid now for the claim that BTC will not be there at expiry.
Collect ~$0.19, risk ~$0.81.

**Break-even win rate = entry cost.** That single identity governs everything.
See the README for the full framing.

## Live status

- **Paper.** `PAPER_TRADING = True`. Paper fills walk the real Kalshi order
  book, so depth and partial fills are live even though the money is not.
- One strategy: `BOUNDARY_NO`. `ENABLE_YES`, `ENABLE_SNIPE`,
  `ENABLE_MISPRICE_NO` all off.
- Branch `model-calibration-and-exit-fixes`, never merged. `main` untouched
  at `4b250ed` since 2026-08-17. **Never run `git merge`.**
- 192 tests across 20 files, all passing.

## Current gates, and why each holds its value

| gate | value | status |
|---|---|---|
| `BOUNDARY_NO_HOURS_MAX` | 0.25 (15 min) | **validated** — only row positive on both halves of the split (+1.9% tune, +6.6% valid). Sliced, only 4.8–15 min makes money (+2.6%); every later slice is negative |
| `BOUNDARY_NO_OVERPRICING_MIN` | 1.25 | lowered from 1.60 — raising it was *selecting for model error* |
| `BOUNDARY_NO_YES_ASK_MAX` | 0.30 | lowered from 0.65. The cheap-NO population loses badly: entries $0.00–0.70 are WR 43% / ROC −36%, while $0.85–0.90 are WR 95% / ROC +8.6%. Two independent datasets agree |
| `NO_STOP` | 0.40 | **reverted from 0.30** — the tightening rested on biased data |
| `NO_EDGE_GONE_MIN_GAIN` | 0.0 | **reverted from 0.15** — gating `edge_gone` cost $52 against real fills |
| `WATCHLIST_ENTRY_DIP` | 0.0 | off — the dip harmed results at every level swept |

## The bias that invalidated a week of analysis (`3b8459a`)

`ladder._window_from()` only considered windows inside `[MIN_HOURS, MAX_HOURS]`,
so a contract under 6 minutes stopped being recorded. Median last observation
was **307 seconds before close**; not one contract was seen within 60s of
expiry. Every counterfactual that resolved contracts by "spot at the last
universe observation" was really reading spot at ~T-5min and calling it
settlement.

What it broke, re-measured against the quotes stream at true `close_time`:

- **ATM/wing study** — 93% WR / +99.8% ROC became 40% WR / −26.7%. Circular:
  entry and "settlement" were read at nearly the same moment. Thread retracted.
- **BOUNDARY_NO** — +3.7% → +1.8% ROC, PF 1.24 → 1.10. Weakened, not overturned;
  the gate sweeps were relative comparisons under identical bias.
- **Exit ladder — INVERTED.** `edge_gone` measured as *costing* $11.57 on the
  biased data; against true settlement it **saves $52.01** across 43 real exits.

**Live trading was never affected** — `self._quotes` is built from
`all_markets`, so held positions were always priced correctly. Only the
recording was blind, which is the worse of the two failures: the bot behaved
correctly while the data used to reason about it did not.

Fix: the expiring window is now recorded but never traded. Rows in
`[0, MIN_HOURS)` reach `record_universe` only.

**Any analysis dated before 2026-08-28 that resolved settlement from the
universe stream is suspect. Re-run it before quoting it.**

## Live paper results — 68 round trips, 08-26 to 08-28

```
total P&L      -$30.74      win rate 63% (43W/25L)
avg win        +$0.83       avg loss  -$2.66
profit factor   0.54        mean entry $0.785
expectancy     -$0.45/trade

edge_gone         n=45   +$23.87   avg +$0.53
time_forced_no    n=8     +$7.50   avg +$0.94
misprice_failed   n=15   -$62.11   avg -$4.14
```

Both profit-taking exits are net positive; **all the damage is 15 stop-outs at
−$4.14 each.** Fifteen losers erase fifty-three winners. That is the
short-premium shape, and it is why hit rate and median trade are both
misleading here — judge on expectancy and profit factor.

## Known-bad reasoning patterns (all learned the hard way)

1. **Dips select losers.** Confirmed three independent times — scale-in,
   dip-adding, watchlist. P(win) decays monotonically with dip depth.
2. **The backtest measures a lane that does not trade.** Under live config it
   produces zero trades. Check `no_trades` in the result JSON before quoting
   any backtest figure.
3. **The model does not beat the Kalshi price.** Five independent tests. Any
   new "edge" that is really model miscalibration will look like this one did.
4. **Multiple comparisons.** ~24 configurations were swept in one session and
   two came out positive. That is expected by chance. Require: same value wins
   both halves of an expiry-clustered split, plus a mechanism.
5. **`ps`/`pgrep` cannot check liveness** — blocked in the sandbox, and a
   blocked check reads as a dead process. Use `recordings/` mtime.

## Open thread — volume as a selector (2026-08-29, revisit in a few days)

Untested hypothesis that produced the most interesting result in a while, and
the only one pointing at *buying* premium rather than selling it. Not
actionable yet; it needs more windows, not more slicing.

Pick the highest-**volume** RANGE contract in the window and buy YES at the
ask. Settlement resolved from the **quotes** stream at `close_time` (never
`universe` — see the blind spot above). 263 windows, 11 contracts, fees both
sides:

```
strategy                  n     WR    ask      ROC     PF     total
highest volume          263    36%   0.32   -14.0%   1.19   +$74.10
nearest to spot (ATM)   263    27%   0.30   -22.3%   0.80  -$106.90
```

**Volume is not a proxy for proximity** — the two selectors agree on only 36%
of windows, and they land $181 apart. That is the finding worth keeping.

By entry price, the cheap end is where the losses are — the same conclusion
`d62fc9d` reached independently from the NO side, so two separate tests now say
Kalshi's far tails are overpriced to a buyer:

```
ask 0.00-0.15   n=92   WR  5%   ROC -50.1%
ask 0.15-0.25   n=24   WR  8%   ROC -68.0%
ask 0.25-0.35   n=43   WR 35%   ROC +15.8%
ask 0.35-0.50   n=41   WR 56%   ROC +32.2%   PF 1.71
ask 0.50-1.01   n=63   WR 78%   ROC  +8.6%
```

**Does NOT pass the split.** The 0.35–0.50 bucket is TUNE n=16 / −1.6% against
VALID n=25 / +53.8%. A negative tune half beside a +54% validation half at that
sample size is the exact shape that has fooled this repo before.

Note ROC −14.0% coexists with **+$74.10** total. Both are right: ROC is per
dollar at risk, so a 6¢ contract losing everything reads −100% while risking
$0.66. At fixed contract count the dollars are what reach the account. Which
metric governs depends on whether sizing is by contract count or by capital —
that question is unresolved and matters here more than anywhere else.

**To revisit:** re-run on windows recorded after `d9afc8e` (the first data that
sees contracts through to expiry), and require the price bucket to hold on both
halves before acting.

## Open items

- **macOS sleep is eating data.** 22% of the last 7 days lost to 12 gaps,
  including 13.0h on 08-29 and 2.8h on 08-28. `caffeinate -s` does not hold on
  battery. Fix: `sudo pmset -b sleep 0 disablesleep 1`.
- `ladder.py` uses `from .config import (...)` — the frozen-import pattern with
  a documented bug history here. Runtime config changes never reach it.
- `spx_vol_calibration.py` is cited by `instrument.py` but does not exist; SPX
  distance constants are BTC values scaled by the 0.0857 σ ratio, uncalibrated.
- 250-wide band grids are effectively unvalidated — 52 of 53 traded contracts
  were 100-wide.
- Backtest/live parity: delayed entry, watchlist, min-hold, depth-confirmed
  exits and the lag filter have no backtest implementation.

## Routine

```bash
# start
KALSHI_RECORD=1 KALSHI_LIVE_VIEW=1 caffeinate -dimsu python3 -m kalshi_btc_bot 2>&1 | tee -a bot_session.log

# liveness — NOT ps
ls -t recordings/universe_*.jsonl.gz | head -1 | xargs stat -f '%Sm'

# back up before the machine sleeps; recordings/ is gitignored and
# unrecoverable (Kalshi publishes no historical order book)
./backup_recordings.sh --verify

# tests
for f in test_*.py; do python3 "$f" >/dev/null || echo "FAIL $f"; done
```

`bot_session.log` is a capture of the live dashboard, not an event stream —
every event repeats once per screen redraw. Count events from `recordings/`,
never by grepping that file.
