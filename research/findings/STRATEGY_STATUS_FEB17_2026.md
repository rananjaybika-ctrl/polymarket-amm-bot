# Strategy Status Report — February 17, 2026

## Executive Summary

Three strategies tested on Polymarket BTC 15-min binary options with validated execution engine (542ms taker delay, 0% maker fee, 50% capital constraint, Polymarket order minimums).

| Strategy | Combined PnL | $/hr | Risk/Trade | WR | Datasets Profitable |
|----------|-------------|------|------------|-----|---------------------|
| **FADE** (backtest) | ~$410 | $2.70 | $0.80 | 94.7% | 5/6 |
| **FADE** (AWS live) | -$29.25 | -$4.16 | $0.80 | ~60% | 0/1 (Feb 10) |
| **Contrarian taker** | +$307 | $1.29 | $0.36 | 39.2% | 4/6 |
| **Directional MM** | +$110 | ~$0.40 | hedge pair | 62.5% | 2/3 train, 0/3 test |

---

## 1. FADE (Aggressive Maker) — Production Config

**Config:** FADE80_3c_ADAPT25_T5_DD20
**Source:** `research/backtests/aggressive_m_v2_grid_search.py`

### Backtest Results (858 trades, 152 hours, 6 datasets)
- $2.70/hr, 241.3% ROI
- 94.7% accuracy at $0.80 threshold
- Entry: MAKER bid at `expensive_ask - $0.03`, 0% fee
- Hold to resolution, no per-trade stop loss
- Adaptive session stop: after 25 trades, if PnL < -$5, enable 20% DD stop

### AWS Live Results (Feb 9-10, paper trading under "AGGRESSIVE" label)
- **Feb 9:** 86 trades, started $170 → ~$232 (+$62) — mixed hedged pairs + naked expensive
- **Feb 10:** 32 trades, started $170 → $140.75 (**-$29.25**) — naked expensive-side bets
  - Two big losses: -$24.30 (30 DOWN @ $0.81, resolved UP) and -$23.70 (30 DOWN @ $0.79, resolved UP)
  - These two wrong calls wiped out all small wins
- **Live trades:** ZERO actual live trades placed (header only)
- **Server:** Stuck in restart crash loop ("address already in use")

### FADE Verdict
- Backtest: Excellent $/hr, but 4:1 risk:reward means 2 wrong calls = catastrophic
- Live: Confirmed — Feb 10 lost $29.25 in 7 hours from just 2 wrong-side resolutions
- **Status: ON HOLD** — needs better risk management or hedge mechanism before re-deployment

---

## 2. Contrarian (Mean-Reversion on Cheap Side) — NEW

**Winner config:** `taker_pb5_ret20_d30`
**Source:** `research/backtests/contrarian_v2_backtest.py` (copied from v2.2 execution engine)

### Signal Logic
1. Track BTC peak/trough from window open
2. After 30s, check for reversal: pullback ≥ 0.005% AND retracement ≥ 20%
3. Buy cheap side (opposite to BTC direction) via TAKER at current ask
4. Hold to resolution — no hedge, no stop loss

### Results (8 configs × 6 datasets)

| Dataset | Trades | Win Rate | PnL | Hours |
|---------|--------|----------|-----|-------|
| IS+OOS2 | 174 | 40.8% | **+$150.77** | 69h |
| OOS3+4 | 143 | 44.1% | **+$185.14** | 47h |
| OOS7 | 63 | 33.3% | -$43.69 | 19h |
| OOS8 | 67 | 37.3% | **+$11.92** | 18h |
| OOS9 | ~88 | ~34% | ~-$6 | 46h |
| OOS10 | 9 | 44.4% | **+$9.28** | 3h |
| **Total** | **544** | **39.2%** | **+$307.06** | **~200h** |

### Key Findings
- **Taker entry crushes maker** for contrarian: +$307 (taker) vs +$9 (best maker). Contrarian signals are time-sensitive; waiting for maker fill loses the edge.
- **Low filters = best:** Loose pullback + low retracement + short delay = most trades and highest WR
- **Entry price ~$0.36** → breakeven WR = 36% → actual WR = 39.2% (3.2% edge)
- **OOS7 weakness:** Strong trending BTC = mean-reversion fails (33% WR)
- **OOS3+4 strength:** Choppy/range-bound BTC = contrarian thrives (44% WR)

### Contrarian Verdict
- **Promising** — profitable on 4/6 datasets with much better risk profile than FADE
- R:R = 1:1.78 vs FADE's 4:1 risk:reward
- Lower $/hr ($1.29 vs $2.70) but far less catastrophic on wrong calls
- **Status: CANDIDATE FOR LIVE TESTING** — needs session stops and regime detection

---

## 3. Directional Market Making (V2.0 → V2.2) — RESEARCH COMPLETE

**Source:** `research/backtests/directional_maker_v2_backtest.py` (v2.0/v2.1), `directional_maker_v2_2_backtest.py` (v2.2)

### Evolution
- **V2.0:** EMA crossover → MAKER bid on predicted winner → hedge on loser. Tested flip/avg_down/gabagool → ALL worse than baseline.
- **V2.1:** Added OBI as signal confirmer → proven identical accuracy with/without (62.5%). Added gabagool mode → catastrophic (-$2,295).
- **V2.2:** Dropped OBI, dropped gabagool, added EMA span sweep. Most realistic execution engine (542ms taker delay, capital constraints, taker fallback at 90s).

### V2.2 Results (18 configs, 3 train + 3 test datasets)

**Train (IS+OOS2, OOS7, OOS9):**
| Dataset | Best Config | PnL | Signal Accuracy |
|---------|------------|-----|-----------------|
| IS+OOS2 (69h) | V22_E500_1200_c2_off1c | -$12.94 | 55.6% |
| OOS7 (19h) | V22_E500_1200_c1_off2c | **+$69.23** | 67.6% |
| OOS9 (46h) | V22_E500_1200_c2_off3c | **+$73.16** | 66.1% |

**Test (OOS3+4, OOS8, OOS10):**
| Dataset | Best Config | PnL | Signal Accuracy |
|---------|------------|-----|-----------------|
| OOS3+4 (47h) | V22_E300_1800_c2_off3c | **-$23.46** | 48.7% avg |
| OOS8 (18h) | V22_E300_1800_c1_off3c | **-$26.15** | 54.9% avg |
| OOS10 (3h) | V22_E200_600_c1_off3c | +$5.89 | 56.7% avg |

### Confirmed Dead Ends
1. **Flip logic = negative EV.** Taker fees (542ms delay + ~1.5% fee) at ~50% accuracy = guaranteed drag. V2.0 proved baseline (no flip) outperforms all spike response modes.
2. **OBI = useless.** Signal investigation on 4 datasets proved OBI adds 0 percentage points to accuracy. The "90.1% OBI accuracy" claim was sample selection bias from a deleted study.
3. **Gabagool = catastrophic.** -$2,295 total across all datasets.
4. **EMA crossover accuracy ceiling = ~62.5%.** This is the fundamental bottleneck. At 62.5% accuracy with hedge pairs costing $0.96-$1.02, the margin is razor-thin and doesn't survive test data.

### Directional MM Verdict
- **Signal accuracy is the bottleneck**, not execution. The execution engine is solid.
- On train data (OOS7, OOS9): works with ~66% accuracy
- On test data (OOS3+4, OOS8): fails with ~49-55% accuracy
- **Status: PAUSED** — needs a fundamentally better signal (>65% accuracy on OOS) to be viable. EMA crossover alone is insufficient.

---

## 4. Strategy Comparison & Recommendation

### Risk-Adjusted Performance
```
FADE:            $2.70/hr, but -$24 on each wrong call (happens ~5% of time)
Contrarian:      $1.29/hr, but -$5.40 max per wrong call (happens ~61% of time)
Directional MM:  $0.40/hr, pairs offset losses but signal too weak on OOS
```

### Recommended Path Forward
1. **Deploy contrarian taker** in paper trading on AWS with session stops (adaptive DD20)
2. **Add regime detection:** Use BTC volatility or trend strength to switch between FADE (trending) and contrarian (choppy)
3. **Directional MM on hold** until a better signal source is found (>65% OOS accuracy needed)
4. **Fix AWS server** — currently in restart loop, no trading happening

### Files Reference
| File | Description |
|------|-------------|
| `research/backtests/contrarian_v2_backtest.py` | Contrarian backtest (v2.2 engine) |
| `research/backtests/directional_maker_v2_2_backtest.py` | Dir MM v2.2 backtest |
| `research/backtests/aggressive_m_v2_grid_search.py` | FADE grid search |
| `research/findings/data/contrarian_v2_all_datasets.csv` | Contrarian results (8 configs × 6 datasets) |
| `research/findings/data/contrarian_v2_results.csv` | Contrarian OOS7 full grid (37 configs) |
| `research/findings/data/directional_maker_v2_2_results.csv` | Dir MM v2.2 train results |
| `research/findings/data/directional_maker_v2_2_test_results.csv` | Dir MM v2.2 test results |
