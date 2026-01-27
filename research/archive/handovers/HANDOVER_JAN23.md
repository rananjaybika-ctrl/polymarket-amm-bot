# Handover: January 23, 2026 - Path 2 Implementation & Direction Analysis

> **COMPLETED (Jan 24, 2026):** Path 2 partial hedge code was DELETED in the Jan 24 restructure.
> The grid search was run (see PATH2_GRID_SEARCH_RESULTS_JAN24.md — also deleted) and showed
> partial hedge provides no meaningful improvement over full hedge. The "Path 2" designation
> now refers to the CONTRARIAN strategy (bet against BTC direction). All code references
> below (run_path2_grid_search, --hedge-ratio, --path2-grid) have been removed from the codebase.

## Session Summary

Implemented Path 2 grid search (partial hedge + short lookbacks) in the modern framework (`volatility_filter_analysis.py`). Ran raw direction accuracy test across lookbacks to validate the Path 2 hypothesis. Updated master plan with OU recalibration analysis.

---

## Key Findings

### 1. Raw Direction Accuracy by Lookback (Static 0.02% threshold, 81.71h data)

| Lookback | Accuracy | 95% CI | Signals/hr | Edge/hr | p-value |
|----------|----------|--------|-----------|---------|---------|
| **500ms** | **58.85%** | [55.3%, 62.3%] | 9.2 | 0.814 | < 0.000001 |
| 300ms | 57.53% | [53.4%, 61.6%] | 6.8 | 0.514 | 0.0004 |
| 400ms | 57.49% | [53.7%, 61.2%] | 8.1 | 0.606 | 0.0001 |
| 1000ms | 56.88% | [53.9%, 59.8%] | 13.4 | 0.924 | < 0.00001 |
| **1400ms** | 56.64% | [54.0%, 59.2%] | **17.0** | **1.126** | < 0.000001 |

**Key insights:**
- 500ms is **most directionally accurate** (58.85%) - validates Path 2's "quality" hypothesis
- 1400ms wins on **edge/hr** (1.126) purely through volume - validates Path 1
- Correct spikes have SMALLER magnitudes (ratio ~0.93)
- Correct signals come ~70-83s LATER in window (avg 525s vs 595s remaining)
- All lookbacks statistically significant (p < 0.001)

### 2. OU Recalibration: NOT NEEDED

- OU params are scale-invariant (log percentage returns)
- AGGRESSIVE already 70% WR on both IS and OOS3
- OU z-score filter is RETIRED; EWMA z-score used now
- Mixing IS + OOS3 regimes risks overfitting

### 3. Strategy Mapping Clarified

```
AGGRESSIVE = "Quality-first" (evolved Path 1)
  - OU threshold, narrow z-zone (0<z<1.5), 180s time-stop
  - 201 trades, 65-70% WR, consistent

BALANCED+EWMA = "Volume-first" (aggressive Path 1 variant)
  - EWMA threshold, wide z-zone (-0.5<z<1.5), 15% price-stop
  - 388 trades, 49-58% WR, regime-dependent

Path 2 (original) = COMPLETELY UNTESTED with modern framework
  - Short lookbacks (300-500ms), partial hedge, aggressive timeout
  - Never had z-score filtering, corrected pricing, or cycling
```

---

## What Was Implemented

### Path 2 Grid Search in `volatility_filter_analysis.py`

**Changes made (keeping Path 1 grid intact):**

1. **Aggressive hedge timeout** (line ~779): Takes market if passive doesn't fill within N seconds
2. **Partial hedge PnL** (line ~813): T1 (hedged) + T2 (rides to resolution) split
3. **`run_path2_grid_search()`** function: 1,080-config grid search
4. **`print_path2_summary()`** function: Results display with Path 2 metrics
5. **CLI args**: `--hedge-ratio`, `--aggressive-timeout`, `--time-stop`, `--path2-grid`

**Grid dimensions (1,080 configs):**

| Dimension | Values | Count |
|-----------|--------|-------|
| Method | ewma, ou | 2 |
| Lookback | 500ms, 950ms, 1400ms (30, 57, 84 ticks) | 3 |
| Hedge ratio | 0.25, 0.50, 0.75 | 3 |
| Stop type | 15% price-stop, 180s time-stop | 2 |
| Cycling | ON, OFF | 2 |
| Z-zone | 0<z<1.5, -0.5<z<1.5, z<1.5, 0<z<2.0, z<1.0 | 5 |
| Aggressive timeout | None, 5s, 10s | 3 |

**KNOWN ISSUE: Cycling + Partial Hedge**

Current code cycles based on when T1 hedges (exit_ts), but T2 naked exposure accumulates across cycles. The correct behavior for partial hedge is **Option 1: no cycling when hedge_ratio < 1.0**. Merge T1, let T2 ride to resolution, don't re-enter. The unhedged T2 IS the continued directional bet — adding more exposure on top defeats the purpose.

**Fix before running:** When `hedge_ratio < 1.0`, force `use_cycling=False` (or skip cycling configs in the grid). This reduces the grid from 1,080 to 540 configs.

**Run command:**
```bash
python research/volatility_filter_analysis.py --path2-grid --zscore-method ewma
```

### Direction Accuracy Test Script

`research/test_lookback_direction.py` - Tests raw spike direction accuracy per lookback with:
- Static 0.02% threshold
- 1-second dedup window
- MIN_TIME_REMAINING = 60s
- Wilson score confidence intervals
- Binomial p-values vs 50%
- Magnitude and time-remaining analysis

---

## AWS Status

| Process | Duration | Ends |
|---------|----------|------|
| `run_data_collection.py --hours 46` | Jan 23 08:16 - Jan 25 06:16 UTC | Jan 25 12:00 PM IST |
| `monitor_0xa5e8_live.py --duration 855m` | Jan 23 15:38 - Jan 24 05:53 UTC | Jan 24 11:23 AM IST |

**SSH access:**
```bash
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221
```

---

## Tomorrow's Plan (Jan 24)

### 1. Run AGGRESSIVE + BALANCED on OOS4

**What:** Validate both configs on fresh OOS4 data (Jan 23-24 collection period).

**Steps:**
```bash
# Download OOS4 data from AWS
scp -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221:~/polymarket-amm-bot/research/observer/grid_obs_20260123.csv research/observer/
scp -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221:~/polymarket-amm-bot/research/binance_hf/btc_prices_20260123_*.csv research/binance_hf/

# Fetch resolutions
python research/fetch_resolutions.py

# Run validation (adapt validate_oos3.py for OOS4 data paths)
python research/validate_oos3.py  # Update paths to OOS4 files
```

**Key questions:**
- Does AGGRESSIVE maintain 65-70% WR?
- Does BALANCED+EWMA hold $26/hr or regress toward IS mean ($3/hr)?
- How many trades in this period? (need 50+ for meaningful stats)

### 2. Analyze 0xa5e8 Monitor Results

**What:** The wallet monitor ran ~21 hours (Jan 23 08:12 - Jan 24 05:53 UTC). Analyze captured trades.

**Steps:**
```bash
# Download monitor log
scp -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221:~/polymarket-amm-bot/logs/monitor_0xa5e8_jan23.log logs/

# Check session summary
tail -50 logs/monitor_0xa5e8_jan23.log
```

**Questions answered by monitor data:**
- Entry delay: consistent ~329s or variable with vol?
- SFP rate: what % of entries have prior-window breakout?
- Skip rate: what % of windows have zero trades (gating)?
- Size: fixed or variable (confidence-based)?
- Round number proximity: clustered near $100 levels?

### 3. Build Path 3: Contrarian Strategy

**What:** New strategy based on 0xa5e8 wallet reverse-engineering. Bets AGAINST BTC direction.

**Key concept:**
- Wait 5-6 minutes into BTC 15-min window
- Bet AGAINST the current direction (contrarian/mean-reversion)
- Entry at cheap side (~$0.30)
- Breakeven WR: only 30% needed (asymmetric payoff)

**Existing work:**
- `scripts/reverse_engineer_wallet.py` - Trade history analysis
- `research/CONTRARIAN_STRATEGY.md` - Backtest results (284 windows, 43% WR, profitable)
- `research/WALLET_0xa5e8_STRATEGY_ANALYSIS.md` - Full strategy breakdown

**Implementation approach:**
- Use monitor data (TA context per trade) to identify gating signals
- Backtest with expanded dataset (500+ hours target)
- Test vol-ratio gate threshold (0.8-1.5)
- Test entry delay (60s vs 180s vs 329s)
- Test Z-score threshold for entry gating

### 4. Run Path 2 Grid Search (if time permits)

```bash
python research/volatility_filter_analysis.py --path2-grid --zscore-method ewma
```

Expected runtime: ~30-60 minutes on 81.71h dataset (1,080 configs).

---

## Files Created/Modified Today

| File | Purpose |
|------|---------|
| `research/OU_RECALIBRATION_PATH_MAPPING_ANALYSIS.md` | Full analysis: OU recalib + strategy mapping |
| `research/test_lookback_direction.py` | Raw direction accuracy test per lookback |
| `research/volatility_filter_analysis.py` | **MODIFIED** - Added Path 2 grid search |
| `research/MASTER_PLAN_TWO_PATHS.md` | **MODIFIED** - Added Path 2 status + OU findings |
| `whats-next.md` | **MODIFIED** - Updated wallet monitor schedule |

---

## Critical Context for Tomorrow

1. **Path 2 grid search is implemented but NOT YET RUN** - the code changes are in `volatility_filter_analysis.py`
2. **Direction test shows 500ms = most accurate** (58.85%) but 1400ms = highest throughput
3. **0xa5e8 monitor ends ~11:23 AM IST Jan 24** - download log immediately after
4. **Data collection runs until Jan 25 12:00 PM IST** - don't interrupt
5. **Partial hedge math**: At 58.85% direction accuracy (500ms), 50% hedge:
   - T1 hedged: safe spread capture
   - T2 unhedged: EV = (0.5885 * 0.50 - 0.4115 * 0.50) * shares = +$0.089/share
   - Combines spread + directional edge
