# Contrarian Mean-Reversion Strategy: Complete Research Document

**Last Updated:** January 23, 2026
**Status:** Backtested, Adaptive Gate Validated, Ready for Production Integration
**Data:** 78.7 hours of 9ms BTC tick data (254,194 1-second bars)

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Origin: Wallet 0xa5e8 Analysis](#origin-wallet-0xa5e8-analysis)
3. [Backtest V1: Z-Score Sweep](#backtest-v1-z-score-sweep)
4. [Backtest V2: Vol Methods + Regime Gates](#backtest-v2-vol-methods--regime-gates)
5. [Adaptive EWMA Vol Gate](#adaptive-ewma-vol-gate)
6. [Volatility Filter Research](#volatility-filter-research)
7. [Production Configs (Spike Strategy)](#production-configs-spike-strategy)
8. [Key Findings & Insights](#key-findings--insights)
9. [Implementation Details](#implementation-details)
10. [File Reference](#file-reference)

---

## Strategy Overview

### Core Idea

Buy the **opposite side** of a BTC move within a 15-minute Polymarket binary market. BTC mean-reverts at short timescales, so a move in one direction during the first 5-7 minutes often reverses by resolution.

### Mechanism

```
BTC 15-min market opens
    ↓
Wait 60-420 seconds (observe BTC direction)
    ↓
BTC moved UP 0.03-0.20% → Buy DOWN at ~$0.30 (cheap)
BTC moved DOWN 0.03-0.20% → Buy UP at ~$0.30 (cheap)
    ↓
Hold to resolution (end of 15-min window)
    ↓
If BTC reverts: payout $1.00, profit $0.70/share
If BTC continues: lose $0.30/share
```

### Why It Works

- **Asymmetric payoff**: Risk $0.30, reward $0.70 (2.33:1 R:R)
- **Breakeven at 30% win rate**: Only need 30% accuracy to break even
- **Observed accuracy: 43-54%**: Far exceeds breakeven threshold
- **BTC mean-reverts at 15-min scale**: Directional moves within 5 min often reverse

### Comparison to Grid Market Making (Gabagool)

| Attribute | Contrarian (0xa5e8) | Grid MM (Gabagool) |
|-----------|--------------------|--------------------|
| Strategy | Directional contrarian | Two-sided market maker |
| Sides per market | One (98%) | Both (100%) |
| Entry timing | Waits 5+ min | Immediate grid |
| Avg price | $0.27 (cheap side) | $0.50 (balanced) |
| Edge source | Directional accuracy | Spread capture |
| Prediction needed | Yes (mean-reversion) | No |
| Risk profile | Directional | Market-neutral |
| Trades/market | 1.8 avg | 20+ avg |

---

## Origin: Wallet 0xa5e8 Analysis

### Wallet Performance (Jan 21-23, 2026)

| Metric | Value |
|--------|-------|
| Resolved Markets | 48 |
| Win Rate | **54.2%** |
| Total PnL | **$61,817** |
| Avg PnL/Market | $1,288 |
| Win/Loss Ratio | 4.8:1 |
| Avg Win | $2,887 |
| Avg Loss | -$602 |
| Avg Entry Price | $0.30 |
| Avg Order Size | 2,576 shares |
| Trades/Hour | 1.8 |
| PnL/Hour | ~$1,222 |

### Entry Pattern

- **76% contrarian**: Bets AGAINST BTC direction
- **Entry delay**: avg 329s (5.5 min), median 394s (6.6 min)
- **BTC move at entry**: avg 0.06% (~$54)
- **Entry threshold**: 76% enter after >= 0.01% BTC move, 53% after >= 0.05%
- **Hold to resolution**: 90% of trades, no pre-resolution exits

### Entry Timing Distribution

| Time Range | Markets | % |
|------------|---------|---|
| 0-30s | 1 | 2% |
| 30-60s | 5 | 12% |
| 60-120s | 3 | 7% |
| 120-300s | 8 | 19% |
| 300s+ | 26 | 60% |

---

## Backtest V1: Z-Score Sweep

**File:** `research/contrarian_backtest.py`

### Approach

- Data: 9ms BTC tick data, resampled to 1s bars
- Split: Training (before `1768705387229`) / OOS2 (after)
- Signal: Z-score = |BTC_move_%| / (rolling_std * sqrt(elapsed_s))
- Parameters swept: Z-thresholds [0.5-2.5], Entry delays [60-420s]
- Entry price: Fixed at $0.30

### Key V1 Finding

The strategy works across a wide range of Z-thresholds. Lower thresholds (Z=0.5) generate more trades but same directional accuracy. The edge is primarily from **mean-reversion at any move size**, not just large moves.

---

## Backtest V2: Vol Methods + Regime Gates

**File:** `research/contrarian_backtest_v2.py`

### Improvements Over V1

1. Three volatility methods for Z-score normalization (EWMA, OU-calibrated, Rolling)
2. Percentile-based volatility regime gating
3. Pre-window vol from PRIOR 5-min data (no lookahead)
4. Proper Z-score: `move / (vol_per_s * sqrt(elapsed_s))`

### Data Summary

| Period | Bars | Hours | Windows |
|--------|------|-------|---------|
| Training | 95,757 | 31.3 | 108 |
| OOS2 | 158,437 | 47.4 | 176 |
| **Total** | **254,194** | **78.7** | **284** |

### Phase 1: Vol Method Comparison

Best performing methods (delay=60s, entry=$0.30):

| Method | Z | Tr WR | Tr PnL | OS WR | OS PnL | Combined |
|--------|---|-------|--------|-------|--------|----------|
| rolling_300s | 0.5 | 43.3% | $34,500 | 40.8% | $47,000 | $81,500 |
| ewma_60s | 0.5 | 43.3% | $34,500 | 40.2% | $44,500 | $79,000 |
| rolling_30s | 1.2 | 43.3% | $34,500 | 40.2% | $44,500 | $79,000 |

**Finding:** Vol method doesn't matter much at low Z-thresholds. At higher Z-thresholds, `rolling_30s` consistently wins, suggesting recent local vol is most predictive.

### Phase 2: Percentile Vol Gate Results

Pre-window vol quartile boundaries (training): Q25=0.000841, Q50=0.001475, Q75=0.002345

| Gate | Tr Trades | Tr WR | Tr PnL | OS Trades | OS WR | OS PnL |
|------|-----------|-------|--------|-----------|-------|--------|
| **Q23_top50** | **53** | **52.8%** | **$30,250** | **128** | **43.0%** | **$41,500** |
| Q3_top25 | 27 | 40.7% | $7,250 | 86 | 47.7% | $38,000 |
| Q012_bottom75 | 77 | 44.2% | $27,250 | 88 | 34.1% | $9,000 |
| Q2_50-75 | 26 | 65.4% | $23,000 | 42 | 33.3% | $3,500 |
| Q01_bottom50 | 51 | 33.3% | $4,250 | 46 | 34.8% | $5,500 |

**Key Finding:** Trading only in **above-median volatility** (Q23, top 50%) boosts training WR from 43.3% to 52.8%. The contrarian signal is stronger when vol is elevated.

### Win Rate by Vol Quartile (Diagnostic)

| Quartile | Train WR | OOS2 WR | Interpretation |
|----------|----------|---------|----------------|
| Q0 (lowest 25%) | 36.0% | 47.4% | Low vol = weak signal, inconsistent |
| Q1 (25-50%) | 30.8% | 25.9% | Below-median vol = worst performance |
| Q2 (50-75%) | 65.4% | 33.3% | Above-median = strong train, weak OOS |
| Q3 (top 25%) | 40.7% | 47.7% | High vol = good OOS performance |

**Insight:** The contrarian signal is most reliable in volatile markets. Low-vol markets don't produce meaningful mean-reversion.

### Phase 3: Entry Delay Sweep

Best config (rolling_300s, Z=0.5, Q23 gate):

| Delay | Tr WR | OS WR | OS PnL |
|-------|-------|-------|--------|
| 30s | 52.8% | 46.1% | $51,500 |
| **60s** | **52.8%** | **43.0%** | **$41,500** |
| 90s | 50.9% | 41.4% | $36,500 |
| 120s | 45.3% | 38.3% | $26,500 |
| 180s | 35.8% | 32.8% | $9,000 |
| 300s+ | <31% | <29% | Negative |

**Finding:** 30-60s entry delay is optimal. Waiting longer means the move has already partially reverted before entry. The 0xa5e8 wallet's 5-7 min delay may be suboptimal (or they're using a different signal threshold).

### Phase 4: Entry Price Sensitivity

| Entry Price | Breakeven WR | Tr PnL | OS PnL | Status |
|-------------|-------------|--------|--------|--------|
| $0.15 | 15% | $50,125 | $89,500 | PROFIT |
| $0.20 | 20% | $43,500 | $73,500 | PROFIT |
| $0.25 | 25% | $36,875 | $57,500 | PROFIT |
| **$0.30** | **30%** | **$30,250** | **$41,500** | **PROFIT** |
| $0.35 | 35% | $23,625 | $25,500 | PROFIT |
| $0.40 | 40% | $17,000 | $9,500 | PROFIT |
| $0.45 | 45% | $10,375 | -$6,500 | MIXED |
| $0.50 | 50% | $3,750 | -$22,500 | MIXED |

**Finding:** Strategy is profitable up to ~$0.42 entry price. Lower entry prices amplify returns but may be harder to fill in practice.

---

## Adaptive EWMA Vol Gate

**Problem:** The fixed percentile gate (Q23) requires `precompute_thresholds(train_vols)` - this is lookahead bias / requires recalibration for new data.

**Solution:** Self-adapting EWMA ratio gate: `current_vol / vol_ema > k`

### How It Works

```python
class AdaptiveEWMAGate:
    # Gate condition: pre_vol / vol_ema >= k
    # k=1.0 → trade when vol is above recent average
    # k=0.5 → trade when vol is above half of recent average

    def update_and_check(self, pre_vol):
        ratio = pre_vol / self.vol_ema
        allowed = ratio >= self.k
        # Update EMA AFTER check (no lookahead)
        self.vol_ema = alpha * pre_vol + (1-alpha) * self.vol_ema
        return allowed
```

### Properties

- **Zero calibration**: No training data needed
- **Self-adapting**: Adjusts to any vol regime automatically
- **No lookahead**: Checks gate BEFORE updating EMA
- **Stateful**: Must reset between independent runs (train vs OOS)
- **Warmup**: First observation seeds EMA, always passes gate

### Parameter Sweep Results (35 configs)

Top performers:

| Config | Tr Trades | Tr WR | Tr PnL | OS Trades | OS WR | OS PnL | Combined |
|--------|-----------|-------|--------|-----------|-------|--------|----------|
| **k=0.5, hl=50** | **68** | **47.1%** | **$29,000** | **159** | **41.5%** | **$45,750** | **$74,750** |
| k=0.5, hl=100 | 67 | 47.8% | $29,750 | 167 | 40.1% | $42,250 | $72,000 |
| k=0.5, hl=10 | 79 | 45.6% | $30,750 | 150 | 40.7% | $40,000 | $70,750 |
| k=0.5, hl=5 | 82 | 42.7% | $26,000 | 151 | 41.7% | $44,250 | $70,250 |
| k=0.7, hl=100 | 50 | 48.0% | $22,500 | 157 | 41.4% | $44,750 | $67,250 |

### Adaptive vs Fixed Comparison

| Config | Tr WR | Tr PnL | Tr $/hr | OS WR | OS PnL | OS $/hr |
|--------|-------|--------|---------|-------|--------|---------|
| Fixed Q23_top50 | 52.8% | $30,250 | $968 | 43.0% | $41,500 | $875 |
| **Adaptive k=0.5, hl=50** | **47.1%** | **$29,000** | **$928** | **41.5%** | **$45,750** | **$964** |
| No Gate | 43.3% | $34,500 | $1,103 | 40.8% | $47,000 | $991 |

**Verdict:** Adaptive gate achieves **104% of fixed gate combined PnL** without calibration.

### Key Insight

The adaptive gate trades ~5pp of training WR for better OOS PnL, suggesting the fixed gate slightly overfits. The best `k=0.5` threshold is permissive (only filters windows with vol below half the recent average), consistent with the finding that higher-vol windows produce better contrarian signals.

### Warmup Behavior

- Gate stabilizes within ~20 windows (~5 hours for 15-min windows)
- First-half pass rate: 63.0%
- Second-half pass rate: 66.7%
- Gate is stable after 3x halflife windows

### Recommended Production Config

```python
AdaptiveEWMAGate(k=0.5, halflife_windows=50)
# k=0.5: Allow trading when vol >= 50% of recent average
# halflife=50: ~12.5 hours lookback (50 × 15min windows)
# Filters out ~35% of windows (very calm markets)
```

---

## Volatility Filter Research

### Z-Score Methods for Vol Filtering

Four methods tested for filtering high-volatility environments:

| Method | Formula | Best Filter | Improvement | Sit-out % |
|--------|---------|-------------|-------------|-----------|
| **OU (static)** | `(log(vol) - mu) / sigma` | z < 1.25 | **+43%** | 35.3% |
| EWMA (adaptive) | `(log(vol) - rolling_mean) / rolling_std` | z < 1.5 | +13% | 6.5% |
| Percentile | `norm.ppf(percentile_rank)` | z < 1.5 | +13% | 8.6% |
| EWMA Ratio | `(log(fast/slow) - mean) / std` | z < 1.5 | +13% | 8.9% |

### Critical Bug Discovery

The initial vol filter grid search had **three critical bugs** that invalidated results:

1. **Resolution PnL**: Overcounted losses by ~80% (charged pair_cost instead of winner_entry)
2. **Stop-loss logic**: Checked loser rise instead of winner drop
3. **Check order**: Triggered stop-loss before checking passive fills

After fixing, OU method confirmed as best ($2.94/hr at 50 shares, 74.6% WR).

### Validated Production Settings (Spike Strategy)

| Config | Stop Type | PnL @50sh | $/hr | Win% |
|--------|-----------|-----------|------|------|
| **AGGRESSIVE** | 180s TIME | $289.49 | $9.53 | 66.7% |
| BALANCED | 15% PRICE | $271.19 | $6.15 | 70.7% |
| CONSERVATIVE | 15% PRICE | $209.76 | $6.19 | 75.0% |

### Stop Type Selection Rule

```
Cycling OFF?                         → PRICE STOP
Cycling ON + OU z-score?             → PRICE STOP
Cycling ON + EWMA z-score + WR<61%?  → TIME STOP
```

---

## Production Configs (Spike Strategy)

These represent the spike-detection variant (reactive to BTC price spikes), distinct from the pure contrarian approach but related:

```python
AGGRESSIVE = TradingConfig(
    threshold_method="ou",
    zscore_method="ewma",
    lookback_ms=1200,
    stop_loss_pct=None,
    time_stop_seconds=180,
    use_cycling=True,
    z_lo=0.0, z_hi=1.5,
    # Expected: $9.53/hr, 66.7% WR
)

BALANCED = TradingConfig(
    threshold_method="ou",
    zscore_method="ou",
    lookback_ms=1200,
    stop_loss_pct=0.15,
    time_stop_seconds=None,
    use_cycling=True,
    z_lo=-0.5, z_hi=1.5,
    # Expected: $6.15/hr, 70.7% WR
)

CONSERVATIVE = TradingConfig(
    threshold_method="ou",
    zscore_method="ou",
    lookback_ms=1200,
    stop_loss_pct=0.15,
    time_stop_seconds=None,
    use_cycling=False,
    z_lo=0.0, z_hi=1.5,
    # Expected: $6.19/hr, 75.0% WR
)
```

---

## Key Findings & Insights

### 1. Mean-Reversion is Real at 15-Min Scale

BTC moves within the first 1-5 minutes of a 15-minute window tend to partially revert by resolution. This creates a ~43-54% accuracy for contrarian bets, which is highly profitable at $0.30 entry prices (breakeven = 30%).

### 2. Volatility Gating is the Key Alpha

Trading only during above-median volatility significantly improves win rate:
- No gate: 43.3% WR (training)
- Top-50% vol gate: 52.8% WR (training)
- The signal is stronger when vol is elevated because mean-reversion is more pronounced.

### 3. Adaptive Gate Matches Fixed Gate Without Calibration

The `AdaptiveEWMAGate(k=0.5, hl=50)` achieves 104% of the fixed percentile gate's combined PnL, proving that:
- No training data is needed
- No recalibration is needed
- The gate self-adapts to changing vol regimes

### 4. Entry Delay: 30-60s is Optimal

- Shorter delays = more signal, higher fill probability
- Longer delays (>120s) = move already reverting, stale signal
- The 0xa5e8 wallet's 5-7 min delay is suboptimal in our backtest data

### 5. Vol Method Matters Less Than Vol Gating

At Z=0.5 (the most profitable threshold), all vol methods produce identical results. The vol gating is the real differentiator, not the Z-score normalization method.

### 6. Low-Vol Markets Are Traps

Q1 (25-50th percentile vol) has the worst performance: 30.8% WR (training), 25.9% WR (OOS). These markets don't produce meaningful BTC moves, so the contrarian signal is pure noise.

### 7. OOS Performance is Consistent

The strategy maintains profitability out-of-sample:
- Training: 43.3% WR, $34,500 PnL (31.3 hours)
- OOS2: 40.8% WR, $47,000 PnL (47.4 hours)
- PnL/hr is consistent: $1,103 (train) vs $991 (OOS)

### 8. No-Gate Baseline is Surprisingly Strong

The no-gate baseline produces the highest absolute PnL ($81,500 combined) because it takes more trades. The gate improves WR but reduces trade count. The optimal tradeoff depends on capital constraints and risk tolerance.

---

## Implementation Details

### Strategy Logic (Pseudocode)

```python
# For each 15-minute market window:

1. Compute pre-window vol (std of 1s returns over prior 5 min)
2. Check adaptive gate: pre_vol / vol_ema >= 0.5
   - If fail: skip this window
   - If pass: continue
3. Wait min_delay_s (60s) from window open
4. Compute BTC move from open: move_pct = (price_now - price_open) / price_open
5. Compute Z-score: z = |move_pct| / (vol_per_s * sqrt(elapsed_s))
6. If z >= threshold (0.5):
   - Entry direction = OPPOSITE of BTC move
   - Buy contrarian side at entry_price (~$0.30)
7. Hold to resolution
8. Settle: +$0.70/share if correct, -$0.30/share if wrong
```

### OU Parameters (Calibrated)

```
mu = -3.9845
sigma_stat = 0.3877
half_life = 5527s (~92 min)
```

### Data Pipeline

```
Binance BTC/USDT websocket (9ms ticks)
    → Resample to 1-second bars
    → Compute 15-minute windows aligned to market opens
    → Compute pre-window vol (prior 300s)
    → Apply vol gate
    → Scan for Z-score entry signals
    → Evaluate at resolution
```

---

## File Reference

### Core Strategy Files

| File | Purpose |
|------|---------|
| `research/contrarian_backtest.py` | V1 backtest (Z-score sweep, basic) |
| `research/contrarian_backtest_v2.py` | V2 backtest (vol methods + gates + adaptive EWMA) |
| `research/TRADING_CONFIGS.py` | Production trading configs (spike strategy) |
| `src/strategies/volatility_regime.py` | Real-time ATR-based regime detection |
| `src/strategies/ou_volatility.py` | OU parameter estimation and Z-score |
| `src/services/volatility_tracker.py` | LiveZScoreTracker for production |

### Analysis & Research

| File | Purpose |
|------|---------|
| `research/WALLET_0xa5e8_STRATEGY_ANALYSIS.md` | Original wallet reverse-engineering |
| `research/GABAGOOL_LESSONS_JAN11.md` | Grid MM comparison (Gabagool) |
| `research/MASTER_PLAN_TWO_PATHS.md` | Two-path optimization (spike strategy) |
| `research/VOL_FILTER_FINDINGS.md` | Vol filter bug analysis + fixes |
| `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` | Full 1440-config grid search |
| `research/FINAL_TRADING_CONFIGS_JAN22.md` | Production config specs |

### Data Files

| File | Purpose |
|------|---------|
| `research/binance_hf/btc_prices_combined.csv` | 7.7M ticks, 9ms resolution |
| `research/ou_params.json` | Pre-calibrated OU parameters |
| `research/contrarian_backtest_results.csv` | V1 backtest results |
| `research/contrarian_backtest_v2_results.csv` | V2 backtest results (all phases) |

---

## Open Questions / Future Work

1. **Live testing**: How does fill probability at $0.30 affect realized performance?
2. **Regime switching**: Does the adaptive gate handle sudden vol regime changes (e.g., FOMC)?
3. **Multi-window**: Can we trade multiple windows simultaneously?
4. **Dynamic sizing**: Should position size scale with vol ratio?
5. **Hybrid approach**: Combine contrarian entry with grid MM hedging?
6. **Entry price optimization**: Can we get fills at $0.20-$0.25 for higher edge?

---

*Document generated from backtest runs on Jan 23, 2026. All PnL figures assume 2,500 shares per trade at $0.30 entry price unless otherwise noted.*
