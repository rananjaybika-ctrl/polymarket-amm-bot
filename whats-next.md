# Next Steps - Polymarket Strategy Development

**Updated**: 2026-01-27

---

## Current State

### Active on AWS (54.170.244.221)

| Process | Duration | Ends |
|---------|----------|------|
| `run_data_collection.py --hours 46` | Jan 23 08:16 UTC - Jan 25 06:16 UTC | **Jan 25 12:00 PM IST** |

**SSH:** `ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221`

**DO NOT INTERRUPT** — AWS data collection ends Jan 25. Let it finish.

### Data Available
- **Training+OOS2**: 81.71 hours (Jan 16-19), 254 markets, 7.7M BTC rows
- **OOS3**: 26.37 hours (Jan 22-23), 90 markets — VALIDATED
- **OOS4**: 24.2 hours (Jan 23-24) — VALIDATED
- **Combined OOS3+OOS4**: ~50.6 hours multi-regime data

### Active Strategies (Post-Optimization Jan 27)

| Strategy | Path | Status | Performance |
|----------|------|--------|-------------|
| **AGGRESSIVE** | Path 1 | **TIME120s_SKIP DEPLOYED** | ~$9.00/hr @50sh (157.4h cross-validated) |
| **CONTRARIAN** | Path 2 | VALIDATED | $618/hr @2500sh ($12.36/hr @50sh equiv), 42% WR |
| ~~BALANCED+EWMA~~ | - | DEPRECATED | $11.17/hr @50sh (regressed from $26.38/hr OOS3) |
| ~~Path 2 partial hedge~~ | - | DELETED | Code and data removed Jan 24 |

**TIME120s_SKIP Optimization (Jan 27, 2026):**
- `time_stop_seconds`: 180 → **120** (+24% hourly rate)
- `min_time_remaining`: 60 → **180** (time_stop + 60s buffer)
- `skip_high_entry`: **true** (skip entries >= $0.90, unhedgeable)

---

## Jan 25 Priorities (After AWS Data Collection Ends)

### Priority 1: Combined OOS3+OOS4 Final Validation

~50.6 hours of multi-regime data. Tighter confidence intervals.

```bash
# Already combined in:
# research/observer/grid_obs_oos3_oos4_combined.csv
# research/observer/btc_prices_oos3_oos4_combined.csv

python research/validate_oos4_all_paths.py  # Update data paths to combined
```

**Expected**: ~290 AGGRESSIVE trades, ~100 CONTRARIAN trades.

### Priority 2: Go-Live Preparation (AGGRESSIVE)

Direction accuracy (72.4%) consistent across 3 OOS periods. Profitable in all.

**Remaining concerns:**
1. Execution latency (passive fill assumption — verify on real orderbook)
2. Position sizing (start with $100-200 bankroll, 5-10 shares per trade)
3. Order placement timing (1200ms lookback = need sub-second reaction)

**Steps:**
1. Set up paper trading with real orderbook data
2. Measure actual fill rates vs backtest assumptions
3. Start with minimum size (5 shares) to validate execution

### Priority 3: CONTRARIAN Execution Design

Simpler execution than AGGRESSIVE (no hedge leg), but needs:
1. Entry price verification ($0.30 actually fillable?)
2. Latency budget (60s+ delay means less time pressure)
3. Position sizing ($0.30 × 2500 = $750 per trade, need bankroll plan)

---

## Strategy Definitions (Jan 27 TIME120s_SKIP)

### Path 1: AGGRESSIVE (Spike Detection + Full Hedge)
- OU threshold, EWMA z-score, 1200ms lookback
- Cycling ON, 0 < z < 1.5, **120s time-stop** (optimized from 180s)
- **min_time_remaining=180s** (time_stop + 60s buffer)
- **skip_high_entry=true** (skip entries >= $0.90)
- Full hedge on loser side
- Cross-validated: ~$9.00/hr @50sh across 157.4 hours, 456 markets

### Path 2: CONTRARIAN (Bet Against BTC Direction)
- $0.30 entry price, 2500 shares per trade
- Adaptive EWMA vol gate (k=0.5, halflife=50)
- Z-score >= 0.5, delay >= 60s
- Hold to resolution (no stops)
- OOS4: $618/hr @2500sh, 42% WR (breakeven = 30%)

---

## Performance Summary (All Periods)

| Period | Hours | AGGRESSIVE $/hr @50sh | AGGRESSIVE Dir% | CONTRARIAN $/hr |
|--------|-------|----------------------|-----------------|-----------------|
| IS (Jan 16-19) | 81.7 | $7.76 | 68.9% | N/A |
| OOS3 (Jan 22-23) | 26.4 | $17.59 | 70.2% | N/A |
| OOS4 (Jan 23-24) | 24.2 | $16.72 | 72.4% | $618/hr @2500sh |

---

## Key Files

| File | Purpose |
|------|---------|
| `research/validate_oos4_all_paths.py` | OOS4 validation (AGGRESSIVE + CONTRARIAN) |
| `research/volatility_filter_analysis.py` | Core backtest with z-score filtering |
| `research/MASTER_PLAN_TWO_PATHS.md` | Strategy definitions (Path 1 + Path 2) |
| `research/TRADING_CONFIGS.py` | Config definitions (Python) |
| `research/CONTRARIAN_STRATEGY.md` | Contrarian strategy research |
| `research/HANDOVER_JAN24_RESTRUCTURE.md` | Jan 24 restructure handover |

---

## Decision Points

1. **AGGRESSIVE go-live**: After combined OOS3+OOS4 confirms edge, start paper trading
2. **CONTRARIAN sizing**: At 2500sh, need $750/trade. Start smaller (500sh = $150/trade)?
3. **Concurrent strategies**: Can run both simultaneously (different markets, different signals)
4. **AWS data**: Use remaining collection for OOS5 validation if needed
