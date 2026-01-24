# MASTER PLAN: Two Paths to Profitable Trading

**Date:** January 18, 2026 (Updated: January 24, 2026)
**Status:** OOS4 VALIDATED — AGGRESSIVE (Path 1) and CONTRARIAN (Path 2) are production strategies
**Objective:** Create repeatable edges through two validated, independent approaches

---

## JAN 24 RESTRUCTURE: NEW PATH DEFINITIONS

### Path Rename (Jan 24)
- **Path 1 = AGGRESSIVE** (spike detection + full hedge, OU threshold, 1200ms, time-stop)
- **Path 2 = CONTRARIAN** (bet against BTC direction, $0.30 entry, 2500 shares, vol gate)
- ~~Old Path 2 (partial hedge)~~: DELETED — code and data removed, never produced viable results

### Combined OOS Performance

| Config | IS (81.7h) | OOS3 (26.4h) | OOS4 (24.2h) | Status |
|--------|-----------|--------------|--------------|--------|
| **AGGRESSIVE** | $7.76/hr | $17.59/hr | **$16.72/hr** | PRIMARY |
| BALANCED+EWMA | $3.06/hr | $26.38/hr | $11.17/hr | DEPRECATED (regime-dependent) |
| **CONTRARIAN** | ~$500-800/hr | N/A | **$618/hr** | VALIDATED |

*All rates at 50sh for AGGRESSIVE, 2500sh for CONTRARIAN*

---

## PATH 1: AGGRESSIVE (Spike Detection + Full Hedge)

### Strategy Philosophy
"Quality-first volume strategy" — detect BTC spikes with OU threshold, enter winner side passively, hedge on loser side, exit via time-stop or passive fill.

### Configuration
```
Threshold Method: OU (adaptive sigmoid on z-score)
Z-Score Method:   EWMA (fully adaptive, no drift)
Lookback:         1200ms (72 ticks at 60Hz)
Stop:             180s TIME (exit if not in profit after 180s)
Cycling:          ON (re-enter after exit)
Z-Zone:           0 < z < 1.5
Hedge:            100% full hedge on loser side
```

### Performance (OOS4 — 24.2 hours, Jan 23-24)
| Metric | @50 shares |
|--------|------------|
| PnL | $404.62 |
| $/hr | **$16.72** |
| Direction Accuracy | 72.4% |
| Trades | 145 |
| Passive Fill Rate | ~55% |
| Time-Stop Exits | ~28% |

### Cross-Validation Summary
| Period | Hours | Trades | $/hr @50sh | Dir Acc |
|--------|-------|--------|------------|---------|
| IS (Jan 16-19) | 81.7 | 90 | $7.76 | 68.9% |
| OOS3 (Jan 22-23) | 26.4 | 84 | $17.59 | 70.2% |
| OOS4 (Jan 23-24) | 24.2 | 145 | $16.72 | 72.4% |

Direction accuracy is remarkably consistent: 68.9% → 70.2% → 72.4% across all periods.

### Why It Works
1. **OU threshold adapts** to volatility regime (sigmoid mapping)
2. **EWMA z-score** doesn't drift (unlike static OU z-score)
3. **Time-stop** lets winning trades ride while cutting losers
4. **Z-zone filter** (0<z<1.5) avoids extreme volatility noise
5. **Full hedge** limits downside to spread cost

---

## PATH 2: CONTRARIAN (Bet Against BTC Direction)

### Strategy Philosophy
"Mean-reversion at the 15-minute scale" — BTC directional moves within 5 minutes often reverse by window end. Buy the cheap side ($0.30) for 2.33:1 reward-to-risk.

### Configuration
```
Entry:            Buy opposite side of BTC direction
Entry Price:      ~$0.30 (the cheap, losing side)
Position Size:    2500 shares per trade
Entry Delay:      >= 60 seconds into window
Vol Gate:         Adaptive EWMA (k=0.5, halflife=50s)
Z-Score Gate:     >= 0.5
Stop:             None (hold to resolution)
Cycling:          OFF (one entry per 15-min window)
```

### Performance (OOS4 — 24.2 hours, Jan 23-24)
| Metric | @2500 shares | @50sh equivalent |
|--------|-------------|-----------------|
| PnL | $14,920 | $298.40 |
| $/hr | **$618** | $12.36 |
| Win Rate | 42% | 42% |
| Trades | 50 | 50 |
| Windows Gated Out | ~35% | ~35% |

### Why It Works
1. **Asymmetric payoff**: Risk $0.30, reward $0.70 (2.33:1 R:R)
2. **Breakeven at 30% WR**: Only need 30% accuracy to profit
3. **Observed 42% WR**: Well above breakeven across IS and OOS4
4. **Adaptive gate**: Filters low-vol noise (35% of windows skipped)
5. **Mean reversion**: BTC 15-min directional moves frequently reverse

### Execution Advantage
- No hedge leg needed (simpler than AGGRESSIVE)
- Single limit order at $0.30
- No time pressure (60s+ delay means slower reaction OK)
- Lower breakeven WR = more robust to adverse conditions

---

## DEPRECATED STRATEGIES

### BALANCED+EWMA (formerly "Path 1 variant")
- **Why deprecated**: $26.38/hr on OOS3 but $3.06/hr in-sample, $11.17/hr OOS4
- **Root cause**: Performance is regime-dependent (higher in choppy micro-vol periods)
- **Conclusion**: Not a stable edge

### Old Path 2 (Partial Hedge)
- **What it was**: Short lookbacks (300-500ms) + partial hedge (25-75%) + aggressive timeout
- **Why deleted**: Never produced >$0.50/hr in any test. Full hedge dominates.
- **Code removed**: Jan 24, 2026 (run_path2_grid_search, hedge_ratio, aggressive_hedge_timeout)

---

## STRATEGY COMPARISON (at equivalent sizing)

| | AGGRESSIVE | CONTRARIAN |
|---|-----------|-----------|
| $/hr @50sh equiv | $16.72 | $12.36 |
| Direction Accuracy | 72.4% | N/A (42% WR) |
| Trades/hour | ~6 | ~2 |
| Risk per trade | Spread cost ($0.01-0.05) | $0.30/share |
| Hedge required | Yes (loser side) | No |
| Execution complexity | High (sub-second timing) | Low (60s+ delay) |
| Breakeven accuracy | ~50% | 30% |
| Capital per trade @50sh | ~$25 | $15 |
| Strategy correlation | Spike-dependent | Direction-dependent |

**Key insight**: These strategies are likely uncorrelated (different signals, different market conditions). Running both simultaneously increases trade count without increasing risk.

---

## GO-LIVE READINESS

### AGGRESSIVE
- [x] Direction accuracy consistent across 3 OOS periods (68-72%)
- [x] Profitable in all test periods
- [ ] Verify execution latency (passive fill assumption)
- [ ] Paper trade with real orderbook data
- [ ] Start with 5-10 shares, scale to 50

### CONTRARIAN
- [x] Win rate (42%) well above breakeven (30%)
- [x] Adaptive gate filters noise successfully
- [ ] Verify $0.30 fills are achievable on Polymarket
- [ ] Determine bankroll for 2500sh trades ($750/trade)
- [ ] Paper trade to measure fill rates

---

## DATA SUMMARY

| Dataset | Hours | Markets | Period | Purpose |
|---------|-------|---------|--------|---------|
| IS (Training+OOS2) | 81.7 | 254 | Jan 16-19 | Grid search, optimization |
| OOS3 | 26.4 | 90 | Jan 22-23 | First validation |
| OOS4 | 24.2 | ~100 | Jan 23-24 | Second validation |
| Combined OOS3+4 | ~50.6 | ~190 | Jan 22-24 | Final confidence |
| AWS Collection | ~46 | TBD | Jan 23-25 | OOS5 (future) |

---

## KEY FILES

| File | Purpose |
|------|---------|
| `research/validate_oos4_all_paths.py` | OOS4 validation (Path 1 + Path 2) |
| `research/volatility_filter_analysis.py` | Core backtest engine |
| `research/spike_param_optimizer.py` | Path 1 parameter optimization |
| `research/TRADING_CONFIGS.py` | Config definitions (Python) |
| `research/CONTRARIAN_STRATEGY.md` | Path 2 research document |
| `research/FINAL_TRADING_CONFIGS_JAN22.md` | Config specs + OOS results |
| `research/HANDOVER_JAN24_RESTRUCTURE.md` | Restructure handover |
