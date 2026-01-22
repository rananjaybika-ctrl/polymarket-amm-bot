# MASTER PLAN: Two Paths to Profitable Trading

**Date:** January 18, 2026 (Updated: January 22, 2026)
**Status:** CONFIGS VALIDATED - Three production configs ready with adaptive stop selection
**Objective:** Create a repeatable edge through one of two validated approaches

### OOS Validation Status: PASSED
- Direction accuracy: 66.7% (threshold ≥55%) ✓
- Mean 60s drop: 0.1367 (range [0.05, 0.15]) ✓
- Passive fill rate: 100% (threshold ≥50%) ✓
- See: `HANDOVER_HEDGE_PRICING_JAN18.md` for details

---

## JAN 22 UPDATE: FINAL VALIDATED CONFIGS

### Grid Search Complete (1440 configs tested on 81.71 hours)
- Volatility filter validated: Z-zone 0<z<1.5 best
- Adaptive threshold method: OU beats EWMA
- **Critical Finding:** Stop type depends on config style

### Three Production Configs

| Config | Stop Type | PnL @50sh | $/hr | Win% |
|--------|-----------|-----------|------|------|
| **AGGRESSIVE** | **180s TIME** | **$289.49** | **$9.53** | 66.7% |
| BALANCED | 15% PRICE | $271.19 | $6.15 | 70.7% |
| CONSERVATIVE | 15% PRICE | $209.76 | $6.19 | 75.0% |

### Stop Type Selection Rule (r = -0.84 correlation)
```
Cycling OFF?                          → PRICE STOP
Cycling ON + OU z-score?              → PRICE STOP
Cycling ON + EWMA z-score + WR<61%?   → TIME STOP
```

### See: Jan 22 Findings Files
- `research/FINAL_TRADING_CONFIGS_JAN22.md` - **Production config specs**
- `research/TIME_STOP_STATISTICAL_ANALYSIS.md` - Statistical analysis
- `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` - Full grid search
- `research/TRADING_CONFIGS.py` - Config definitions (Python)

---

## THE TWO PATHS

| Path | Philosophy | Lookbacks | Configs | Focus |
|------|------------|-----------|---------|-------|
| **Path 1: Volume** | Many signals, quick in/out | 800ms, 1000ms, 1200ms | 2,880 | Entry Order Pulling |
| **Path 2: Quality** | Few signals, asymmetric R:R | 300ms, 500ms, 600ms | 4,896 | Partial Hedge + Aggressive Hedge |

---

## PATH 1: VOLUME + ENTRY PULLING

### Concept
Use longer lookbacks (800ms, 1000ms, 1200ms) to detect MORE signals.
Protect capital with aggressive entry order pulling.
Quick in/out - if entry doesn't fill fast, cancel and wait for next signal.

### Parameters Tested
| Parameter | Values |
|-----------|--------|
| Lookbacks | 800ms, 1000ms, 1200ms (48, 60, 72 ticks) |
| Entry Pull Timeout | 3s, 5s, 7s, 10s, 15s, 20s, 25s, 30s |
| Order Pulling | ON and OFF (to compare) |
| Stop Loss | 3%, 5%, 7%, 12%, None |
| Target Shares | 5, 10, 15, 30 |
| Grid Levels | 1, 2, 3 |
| Hedge Ratio | 100% (full hedge) |

### Run Command
```bash
cd /Users/rananjaybika/polymarket-amm-bot
python research/spike_param_optimizer.py --path path1 --output research/path1_results.csv
```

---

## PATH 2: QUALITY + PARTIAL HEDGE

### Concept
Use shorter lookbacks (300ms, 500ms, 600ms) for HIGHER QUALITY signals.
Test partial hedging - let some portion ride to resolution.
Add aggressive hedge option - take market if passive doesn't fill quickly.
Use tighter stop-losses to cut losses faster.

### Parameters Tested
| Parameter | Values |
|-----------|--------|
| Lookbacks | 300ms, 500ms, 600ms (18, 30, 36 ticks) |
| Hedge Ratio | 25%, 50%, 75%, 100% |
| Aggressive Hedge Timeout | None, 5s, 10s, 15s |
| Stop Loss | 3%, 5%, 7%, 12%, None |
| Target Shares | 5, 10, 15, 30 |
| Grid Levels | 1, 2, 3 |
| Order Pulling | ON and OFF |

### Safety Rule
Partial hedge (< 100%) REQUIRES stop-loss for T2 protection.

### Run Command
```bash
cd /Users/rananjaybika/polymarket-amm-bot
python research/spike_param_optimizer.py --path path2 --output research/path2_results.csv
```

---

## DATA SUMMARY

| Metric | Value |
|--------|-------|
| Total Hours | 18.86 |
| Valid Markets | 65 |
| Binance Files | 5 (1.46M rows) |
| Sessions | 2 (with 6.22h gap) |

---

## FIXED PARAMETERS (Hardcoded)

| Parameter | Value |
|-----------|-------|
| SIGNAL_TYPE | enhanced |
| MIN_TIME | 60 seconds |
| SPIKE_THRESHOLD (base) | 0.02 |
| VELOCITY_CONFIRM_THRESHOLD | 0.10 |
| ENHANCED_SCORE_THRESHOLD | 0.02 | v2 formula: spike_mag * velocity_bps |
| DROP_MULTIPLIER | 0.50 | **Updated** - see HEDGE_PRICING_FINDINGS.md |
| DROP_INTERCEPT | 0.08 | **Updated** - was underpredicting drops (0.03 vs actual 0.10) |
| DROP_REGIME_BONUS | {LOW: 0, MEDIUM: 0.01, HIGH: 0.02} | **New** - regime adjustment |
| TARGET_PAIR_COST | 0.99 |
| MIN_CYCLE_GAP_MS | 1000ms |
| CAPITAL_LIMIT | $170 |
| Adaptive Volatility | ON |

### Adaptive Volatility Thresholds
| Regime | Spike Threshold |
|--------|-----------------|
| LOW | 0.010% |
| MEDIUM | 0.020% |
| HIGH | 0.035% |

---

## IMPLEMENTATION CHANGES MADE

### Removed (Useless)
- `order_pull_timeout` (40s hedge timeout) - was never used

### Added
- `aggressive_hedge_timeout` - take market if passive doesn't fill (None, 5s, 10s, 15s)
- Tighter stop-losses (3%, 5%) added to existing (7%, 12%, None)
- Entry pull timeouts expanded (3s, 5s, 7s, 10s, 15s, 20s, 25s, 30s)
- Path-specific lookback filtering

### Updated
- Path 1: Tests 800ms, 1000ms, 1200ms lookbacks only
- Path 2: Tests 300ms, 500ms, 600ms lookbacks only
- OptResult tracks `aggressive_hedge_pct` and `aggressive_pnl`
- **Hedge pricing formula recalibrated** - see `HEDGE_PRICING_FINDINGS.md`
  - Old formula (0.68 * spike + 0.01) severely underpredicted drops
  - New formula: 0.08 + 0.50 * spike + regime_bonus
  - Analysis showed spike_magnitude has ~0 correlation with actual 60s drops

---

## SUCCESS CRITERIA

| Metric | Path 1 Target | Path 2 Target |
|--------|---------------|---------------|
| $/hr | > $0.90 | > $0.48 |
| Win Rate | > 70% | > 50% |
| Max Drawdown | < 10% | < 15% |
| Trades/hr | > 3 | > 1 |

---

## COMMANDS

### Run Path 1 (Entry Pulling)
```bash
python research/spike_param_optimizer.py --path path1 --output research/path1_results.csv
```

### Run Path 2 (Partial Hedge)
```bash
python research/spike_param_optimizer.py --path path2 --output research/path2_results.csv
```

### Run Both in Parallel (separate terminals)
```bash
# Terminal 1
python research/spike_param_optimizer.py --path path1 --output research/path1_results.csv

# Terminal 2
python research/spike_param_optimizer.py --path path2 --output research/path2_results.csv
```

---

## FILES REFERENCE

### Core Strategy Files
| File | Purpose | Lines |
|------|---------|-------|
| `research/spike_param_optimizer.py` | Grid search optimizer | ~1300 |
| `src/strategies/enhanced_spike.py` | Live spike strategy | 1664 |
| `src/strategies/volatility_regime.py` | Regime detector | 746 |
| `src/strategies/enhanced_momentum.py` | Partial hedge (ready) | 708 |
| `scripts/run_paper_bot.py` | Live trading bot | ~5000 |

### Data Files
| File | Purpose |
|------|---------|
| `research/binance_hf/btc_prices_20260116_194712.csv` | 60Hz Binance (8.19h) |
| `research/binance_hf/btc_prices_20260117_101156.csv` | 60Hz Binance (0.28h) |
| `research/binance_hf/btc_prices_20260117_103132.csv` | 60Hz Binance (1.73h) |
| `research/binance_hf/btc_prices_20260117_121445.csv` | 60Hz Binance (6.60h) |
| `research/binance_hf/btc_prices_20260117_185159.csv` | 60Hz Binance (2.00h) |
| `research/observer/grid_obs_*.csv` | 5Hz observer data |
| `research/observer/market_resolutions_verified.csv` | Verified outcomes |

### Research Files
| File | Purpose |
|------|---------|
| `research/enhanced_spike_60hz_optimized.py` | Main backtest script |
| `research/enhanced_momentum_backtest.py` | Partial hedge backtest |
| `research/hedge_pricing_analysis.py` | Regression analysis for hedge pricing |
| `research/validate_oos.py` | **OOS VALIDATED** - Out-of-sample validation script |
| `research/fetch_resolutions.py` | Fetch verified resolutions from Polymarket API |
| `research/HANDOVER_60HZ_BACKTEST_JAN17.md` | Session findings |
| `research/HEDGE_PRICING_FINDINGS.md` | Hedge formula recalibration results |
| `research/HANDOVER_JAN18.md` | **OOS VALIDATED** - Full session handover (hedge pricing + optimizer) |
| `research/MASTER_PLAN_TWO_PATHS.md` | This file |

### Jan 22 Findings (Volatility Filter + Adaptive Stops)
| File | Purpose |
|------|---------|
| `research/FINAL_TRADING_CONFIGS_JAN22.md` | **PRODUCTION CONFIGS** - Three validated configs with stop specs |
| `research/TRADING_CONFIGS.py` | Python config definitions for backtesting |
| `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` | Full 1440-config grid search analysis |
| `research/TIME_STOP_STATISTICAL_ANALYSIS.md` | Statistical analysis of time vs price stops |
| `research/TIME_BASED_STOP_FINDINGS.md` | Time-based stop test results |
| `research/volatility_filter_analysis.py` | Backtest with z-score filtering |
| `research/validate_three_configs.py` | Validation script for 3 configs |
| `research/three_config_validation_results.csv` | Validation output |
| `research/vol_filter_grid_results_all_combined.csv` | Full grid search results (1440 rows) |
| `research/time_stop_top50_results.csv` | Time-stop vs price-stop comparison |
| `research/stop_out_analysis_results.csv` | Stop-out breakdown for top 10 |
| `src/services/volatility_tracker.py` | **LiveZScoreTracker** for production |
