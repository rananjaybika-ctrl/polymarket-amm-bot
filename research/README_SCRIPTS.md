# Research Scripts Reference

**Last Updated:** January 16, 2026

---

## Script Categories

### 1. SINGLE-ENTRY BACKTESTS (No Cycling)
One trade per market maximum. Use these for baseline parameter testing.

| Script | Purpose | Key Output |
|--------|---------|------------|
| `verify_stoploss_5vs7.py` | **CLEAN** comparison of 5% vs 7% stop-loss | $1.18/hr with 7% SL |
| `stoploss_5pct_comparison.py` | Compare Zone 4 vs Zone 5-6 filters | Zone 5-6 is better |
| `zone56_stoploss5_backtest.py` | Zone 5-6 + 5% stop-loss test | Single-entry baseline |
| `tonight_session_analysis.py` | Real-time validation on new data | Live data test |

### 2. CYCLING BACKTESTS (Multiple Entries per Market)
Re-enters after each hedge completes. Tests cycling impact.

| Script | Purpose | Key Output |
|--------|---------|------------|
| `cycling_backtest.py` | **PRIMARY** - Multiple entries with cycling | **$14.94/hr** (10x trade multiplier!) |
| `stoploss_2pct_analysis.py` | Test 2%, 3%, 5%, 7%, 10%, 15% stop-loss | 5% optimal with cycling |
| `comprehensive_analysis.py` | Full analysis with all observer data | Comparison tool |

### 3. VELOCITY ANALYSIS (Signal Quality)
Tests if velocity predicts short-term price movement.

| Script | Purpose | Key Finding |
|--------|---------|-------------|
| `velocity_price_deep_analysis.py` | Deep velocity vs price analysis | 97% loser drop rate |
| `velocity_correct_analysis.py` | Short-term velocity correctness | 77% accuracy (good conditions) |
| `velocity_protected_mm.py` | Velocity-protected market making | Asymmetric offsets help |
| `velocity_assisted_mm.py` | Test asymmetric bid strategies | Winner +0.01, Loser -0.07 |
| `two_sided_grid_backtest_v4.py` | Production backtest | 97% loser drops $0.28+ |

### 4. PARAMETER OPTIMIZATION
Sweep parameters to find optimal values.

| Script | Purpose | Key Finding |
|--------|---------|-------------|
| `offset_optimization.py` | Test winner/loser offset combos | -0.12 loser offset optimal |
| `optimize_full_strategy.py` | Full parameter sweep template | Needs cycling flag added |
| `zone56_detailed_analysis.py` | Loser offset optimization | -0.12 sweet spot |

### 5. SIGNAL ANALYSIS (No Backtesting)
Pure analysis scripts - no PnL calculation.

| Script | Purpose | Key Finding |
|--------|---------|-------------|
| `zone56_frequency_per_market.py` | Count Zone 5-6 events | 77% markets have 2+ events |
| `spread_analysis.py` | Analyze bid/ask spreads | $0.02 average spread |
| `unhedged_analysis.py` | Analyze unhedged outcomes | Different strategy (passive MM) |

### 6. GABAGOOL ANALYSIS
Scripts analyzing Gabagool22's trading strategy.

| Script (in scripts/) | Purpose |
|---------------------|---------|
| `gabagool_earliest_markets.py` | Find earliest Gabagool activity |
| `gabagool_deep_analysis.py` | Comprehensive strategy analysis |
| `gabagool_imbalance_analysis.py` | Imbalance tracking |
| `gabagool_trade_sequence_analysis.py` | Order timing patterns |

---

## Key Strategy Configurations

### Current Best Parameters (Velocity-Gated)
```python
MIN_VELOCITY_BPS = 0.50    # Zone 5-6 only
WINNER_OFFSET = +0.01      # Aggressive entry at ASK
LOSER_OFFSET = -0.12       # Passive hedge
STOP_LOSS_PCT = 0.07       # 7% (or 5%, nearly identical)
SHARES_PER_SIDE = 15       # Target position
MIN_TIME = 120             # Don't enter with <2min left
ENABLE_CYCLING = True      # Cycling provides 10x trade multiplier!
```

### Performance Summary (Jan 16, 2026 - 101 markets, 25.2 hours)

| Mode | Hourly Rate | Why |
|------|-------------|-----|
| No cycling (1 entry) | $1.30/hr | Single trade per market |
| **With cycling** | **$14.94/hr** | **10x trades, 34% unhedged at 100% accuracy** |
| Gabagool style (passive MM) | ~$2-3/hr | Maker rebates + grid posting |

**Trade Breakdown (With Cycling):**
| Type | Count | % | Avg PnL |
|------|-------|---|---------|
| Passive hedge | 210 | 25% | +$1.38 |
| Stop-loss hedge | 341 | 41% | -$0.80 |
| Unhedged (100% correct) | 281 | 34% | +$1.28 |

---

## Data Files

Located in `research/observer/`:
- `spread_capture_obs_*.csv` - Observer output files
- Format: 26 or 35 columns depending on version

Located in `research/`:
- `gabagool_earliest_trades_*.csv` - Gabagool trade history
- `gabagool_earliest_markets_*.csv` - Gabagool market summaries

---

## Quick Reference: Which Script for What

| Task | Use This Script |
|------|-----------------|
| Test stop-loss thresholds | `verify_stoploss_5vs7.py` |
| Compare cycling vs no-cycling | `cycling_backtest.py` (has both) |
| Test velocity prediction | `velocity_correct_analysis.py` |
| Analyze Gabagool strategy | `scripts/gabagool_deep_analysis.py` |
| Optimize offsets | `offset_optimization.py` |
| Validate on live data | `tonight_session_analysis.py` |

---

## Known Issues

1. **cycling_backtest.py** - ✅ Fixed: uses ASK for winner fill, checks to 10s, deduplicates markets, tracks unhedged resolution
2. **unhedged_analysis.py** - Uses DIFFERENT strategy (passive both sides) - not our strategy
3. **stoploss_2pct_analysis.py** - Still has cycling, doesn't deduplicate
4. **comprehensive_analysis.py** - May show lower results if not loading all CSV files

## Key Insight (Jan 16, 2026)

**Why 100% of unhedged trades are velocity correct:**
- If velocity prediction is WRONG → winner price drops → stop-loss triggers (counted as stop-loss)
- If velocity prediction is RIGHT → winner price holds → stays unhedged → resolves to $1

This selection bias means unhedged = guaranteed profit, which explains the high hourly rate.
