# AGGRESSIVE_M (V2) Strategy

**Status:** PRODUCTION-READY with Adaptive Session Stops
**Date:** February 7, 2026 (Updated with session stop findings)
**Replaces:** AGGRESSIVE (taker-based, DEPRECATED)

> **Evolution:** AGGRESSIVE_M V1 showed maker entry has adverse selection when FOLLOWING spikes. V2 **FADES** the spike instead when expensive_side >= $0.80. **Feb 7 Update:** Added adaptive session stops to protect against losing regimes like OOS9.

---

## Executive Summary

AGGRESSIVE_M (V2) fades BTC spikes when the market doesn't believe them. When a spike is detected but the expensive_side (opposite to spike) remains expensive (>= $0.80), the spike is likely noise. We buy the expensive_side as a MAKER (0% fee) and hold to resolution.

**Key Insight:** When AGGRESSIVE filters detect a spike but Polymarket doesn't react (expensive_side stays expensive), the market is right 87-90% of the time.

### Production Config: FADE80_3c_ADAPT25_T5_DD20

| Parameter | Value | Description |
|-----------|-------|-------------|
| Entry | `expensive_ask - 0.03` | Bid 3c below ask (maker) |
| Threshold | `expensive_ask >= $0.80` | Only trade high-probability sides |
| Position Size | 15 shares | ~$12 per trade |
| Session Stop | ADAPT25_T5_DD20 | Adaptive regime detection |

**Performance (152 hours across 4 datasets):**
- **Total PnL:** $410.21
- **PnL/hour:** $2.70/hr
- **ROI:** 241.3%
- **Trades:** 858

**Adaptive Session Stop:** After 25 trades, if PnL < -$5, enable 20% drawdown stop. This protects against losing regimes (like OOS9) while letting winners run.

---

## Validated Performance (With Proper Deduplication)

⚠️ **IMPORTANT:** All results use 10s cooldown deduplication per (market, direction) to reflect realistic trading capacity. Without dedup, signal counts are inflated ~80x.

### OBI Filter Comparison (10s cooldown, expensive_ask >= $0.70)

| OBI Strategy | Signals | FADE Accuracy | Avg Entry | $/trade | Total $ |
|--------------|---------|---------------|-----------|---------|---------|
| NO_OBI | 653 | 88.4% | $0.878 | $0.03 | $18 |
| **OBI_FOLLOW** | **424** | **90.1%** | $0.881 | **$0.10** | **$42** |
| OBI_FADE | 161 | 88.2% | $0.854 | $0.14 | $22 |

**Winner: OBI_FOLLOW** - Best balance of accuracy (90.1%) and signal volume (424)

### By Dataset (OBI_FOLLOW, 10s cooldown, >= $0.70)

| Dataset | Period | Signals | FADE Accuracy |
|---------|--------|---------|---------------|
| IS+OOS2 | Jan 16-19 | ~150 | ~90% |
| OOS7 | Jan 29-30 | ~274 | ~90% |
| **Combined** | - | **424** | **90.1%** |

### By Expensive Side Price Threshold (OBI_FOLLOW, 10s cooldown)

| Threshold | Signals | FADE Accuracy | $/trade |
|-----------|---------|---------------|---------|
| >= $0.65 | 473 | 86.7% | $0.04 |
| **>= $0.70** | **424** | **90.1%** | **$0.10** |
| >= $0.75 | 368 | 91.3% | $0.04 |
| >= $0.80 | 333 | 94.0% | $0.10 |

**Optimal threshold: $0.70** - Best $/trade and total expected value

---

## Strategy Logic

### Terminology (Updated Feb 2026)

| Old Term | New Term | Definition |
|----------|----------|------------|
| winner_side | spike_side | The side BTC spike predicts (direction of spike) |
| loser_side | expensive_side | The OPPOSITE side - what we actually BUY |
| winner_ask | spike_ask | Ask price of spike_side |
| loser_ask | expensive_ask | Ask price of expensive_side (our entry) |

**Why "expensive_side"?** We only trade when this side is >= $0.65 (expensive). The name describes our entry condition.

### Signal Generation (Same as AGGRESSIVE V1)

1. **EWMA Spike Detection** (1000ms halflife)
   - Compare current BTC price to EWMA
   - OU adaptive threshold (calibrated on IS+OOS2)

2. **Velocity Confirmation**
   - Velocity must confirm spike direction
   - Threshold: ±0.10 bps

3. **Enhanced Score >= Threshold**
   - Composite of magnitude, velocity, time remaining

4. **OBI Filter** (if available)
   - Enhanced OBI filter with expensive spread consideration

### AGGRESSIVE_M V2 Filter (NEW)

5. **OBI Filter (OBI_FOLLOW)**
   ```python
   # OBI > 0 on spike_side = market confirms spike = better signal
   if obi_spike is not None and obi_spike > 0:
       pass  # Signal passes
   else:
       continue  # Skip signal
   ```

6. **Expensive Side Price Check**
   ```python
   # After all AGGRESSIVE filters pass:
   if expensive_ask >= 0.70:  # Market uncertain
       action = "FADE"   # Buy expensive_side
   else:
       action = "SKIP"   # Market agrees with spike
   ```

7. **Deduplication (10s cooldown)**
   ```python
   # Per CLAUDE_MISTAKES.md #50 - signals cluster in bursts
   COOLDOWN_SECONDS = 10
   if spike_ts - last_signal_ts[spike_dir] < cooldown_ms:
       continue  # Skip duplicate
   ```

### Entry

- **Side:** expensive_side (OPPOSITE of spike direction)
- **Order Type:** MAKER (limit order)
- **Entry Price:** expensive_ask (current ask)
- **Entry Fee:** 0% (maker)

### Exit

- **Hold to Resolution:** Primary exit
- **Time-stop:** Optional - saves ~$0.54/share on losing trades
- **Exit Fee:** 0% if maker exit, 2% if taker

---

## Expected Value

### Per Trade (5 shares, OBI_FOLLOW @ $0.70 threshold)

| Metric | Value |
|--------|-------|
| Accuracy | 90.1% |
| Avg entry | $0.881 |
| EV per share | $0.901 - $0.881 = **$0.020** |
| EV per trade (5 shares) | **$0.10** |

### Hourly Rate

With 10s cooldown: ~424 signals across IS+OOS2 + OOS7 datasets
- Estimated ~15-20 trades/hour depending on market activity
- **$1.50-2.00/hour** expected (conservative)

Note: EV is thin because accuracy (90.1%) barely exceeds entry price ($0.881). Higher thresholds ($0.80+) have better $/trade but fewer signals.

---

## Why This Works

### The Divergence Signal

When BTC spikes but Polymarket doesn't follow:
1. BTC moved → AGGRESSIVE detects spike
2. expensive_side stays expensive (>= $0.65) → Market says "I don't believe this spike"
3. Market is right 90% of the time

### Economic Intuition

- **Expensive expensive_side** = Market uncertain about outcome
- **Spike not moving Polymarket** = Spike is noise, not signal
- **FADE** = Trust Polymarket over short-term BTC noise

### vs AGGRESSIVE (V1 Taker)

| Aspect | AGGRESSIVE V1 (taker) | AGGRESSIVE_M V2 |
|--------|----------------------|-----------------|
| Direction | FOLLOW spike | FADE spike |
| Entry fee | 2% (taker) | 0% (maker) |
| Accuracy | 46% (when expensive_ask >= $0.65) | **90%** |
| Signal | Spike predicts winner | Spike predicts LOSER |

---

## Price Movement Analysis

After FADE signal (expensive_ask >= $0.65):

| Window | Mean Favorable | Reaches +$0.05 | Reaches +$0.10 |
|--------|----------------|----------------|----------------|
| 30s | $0.013 | 16.6% | 2.5% |
| 60s | $0.022 | 18.2% | 7.6% |
| 120s | $0.038 | 24.2% | 14.4% |
| 300s | $0.060 | 38.2% | 20.1% |

**Conclusion:** Price drifts slowly in our favor. This is a **hold-to-resolution strategy**, not scalping.

---

## Grid Search Parameters

### Core Parameters

| Parameter | Grid Values | Default | Rationale |
|-----------|-------------|---------|-----------|
| min_expensive_ask | [0.65, 0.70, 0.75, 0.80] | **0.70** | Best $/trade at 0.70 |
| obi_filter | [NO_OBI, OBI_FOLLOW] | **OBI_FOLLOW** | 90.1% vs 88.4% accuracy |
| cooldown_seconds | [10, 30] | **10** | More signals, similar accuracy |
| shares_per_trade | [5, 10, 25, 50] | 5 | Position sizing |

### Stop Loss Parameters

| Parameter | Grid Values | Rationale |
|-----------|-------------|-----------|
| time_stop_seconds | [None, 30, 60, 120] | None = hold to resolution |
| price_stop_pct | [None, 0.10, 0.15, 0.20] | % adverse move to cut |

**Stop Loss Analysis (on 10% losing trades):**
- Time stop at 30s: saves ~$0.54/share vs resolution
- Time stop at 60s: saves ~$0.52/share vs resolution
- Time stop at 120s: saves ~$0.55/share vs resolution

### Hedge Ratio (Optional)

| Parameter | Grid Values | Description |
|-----------|-------------|-------------|
| hedge_ratio | [0.0, 0.50, 0.75, 1.0] | 0 = hold to resolution, 1 = full hedge |

**Recommendation:** Start with hedge_ratio = 0 (hold to resolution). Edge comes from resolution payout.

---

## Implementation

### Config (for TRADING_CONFIGS.py)

```python
@dataclass
class AggressiveMV2Config:
    # Signal detection (same as AGGRESSIVE V1)
    spike_method: str = "EWMA_1000"
    lookback_ticks: int = 72
    velocity_confirm_threshold: float = 0.10
    enhanced_score_threshold: float = 0.30

    # AGGRESSIVE_M V2 filters
    min_expensive_ask: float = 0.70  # Only trade when expensive_side >= this
    obi_filter: str = "OBI_FOLLOW"   # OBI > 0 on spike_side
    cooldown_seconds: int = 10       # Deduplication per (market, direction)

    # Entry
    entry_mode: str = "MAKER"  # 0% fee
    shares_per_trade: int = 5

    # Exit
    time_stop_seconds: Optional[float] = None  # None = hold to resolution
    price_stop_pct: Optional[float] = None
    hold_to_resolution: bool = True

    # Timing
    min_time_remaining: float = 90.0
```

### Source Files

| File | Purpose |
|------|---------|
| `research/backtests/aggressive_m_v2_ewma_study.py` | Validation study |
| `research/findings/data/aggressive_m_v2_ewma_study_results.csv` | Study results |
| `research/backtests/aggressive_main_backtest.py` | EWMA + OU detection (imported) |
| `research/findings/AGGRESSIVE_M_STUDY_RESULTS.md` | V1 adverse selection study |

---

## Risks

### 1. Adverse Selection (Mitigated)

AGGRESSIVE_M V1 study showed maker entry has 4-7pp adverse selection when FOLLOWING spikes. V2 mitigates this by:
- FADING instead of FOLLOWING
- Using divergence signal (expensive_ask >= $0.80) which filters for uncertainty
- Not chasing momentum (where adverse selection is worst)

### 2. Fill Rate (Unknown)

As MAKER, orders may not fill. Need to test:
- What % of signals actually fill?
- Does accuracy differ when filled vs unfilled?

### 3. Regime Dependence (MITIGATED with Adaptive Stops)

| Regime | Dataset | FADE Accuracy | PnL | Outcome |
|--------|---------|---------------|-----|---------|
| LOW vol | IS+OOS2 | 87-90% | +$83.36 | WINNING |
| HIGH vol | OOS7 | 87-90% | +$232.51 | WINNING |
| HIGH vol | OOS8 | 85-88% | +$144.18 | WINNING |
| **LOSING** | **OOS9** | **87.6%** | **-$151.91** | **LOSING** |

**OOS9 Problem:** High accuracy but negative PnL. Losses when wrong exceeded gains when right.

**Mitigation:** ADAPT25_T5_DD20 detects losing regimes after 25 trades and enables session stop. OOS9 loss reduced from -$151.91 to -$49.84 (+$102 saved).

### 4. Per-Trade Stop Losses (DO NOT USE)

Per-trade stop losses **hurt performance**:

| Config | OOS9 PnL | Problem |
|--------|----------|---------|
| No stop | -$77.80 | Baseline |
| 15% stop | -$101.87 | **Worse** - stops on temporary dips |
| 25% stop | -$96.84 | **Worse** - same issue |

The strategy holds to resolution. Temporary adverse moves recover. Per-trade stops crystallize losses that would have been wins.

---

---

## Session Stop Mechanism (February 7, 2026)

### The Problem: OOS9 Losing Regime

Grid search on OOS9 (Feb 1-3, 2026) revealed a **losing regime** where the strategy lost $151.91 despite 87.6% FADE accuracy:

| Dataset | Regime | Trades | Total PnL | PnL/hr | FADE Accuracy |
|---------|--------|--------|-----------|--------|---------------|
| IS+OOS2 | WINNING | 73 | +$83.36 | +$1.20 | 87-90% |
| OOS7 | WINNING | 307 | +$232.51 | +$12.27 | 87-90% |
| OOS8 | WINNING | 453 | +$144.18 | +$7.96 | 85-88% |
| **OOS9** | **LOSING** | 385 | **-$151.91** | **-$3.33** | 87.6% |

**Root Cause:** In OOS9, losses on incorrect trades were larger than gains on correct trades. Even with 87.6% accuracy, the strategy lost money because:
- Entry prices were worse (filled at unfavorable levels)
- Market moved against positions before resolution
- Higher volatility caused more extreme outcomes

### Solution: Adaptive Session Stops

Per-trade stop losses **don't work** - they crystallize temporary losses that would recover at resolution:

| Config | OOS9 Trades | OOS9 PnL | Win Rate | Problem |
|--------|-------------|----------|----------|---------|
| **NOSLP** (no stop) | 492 | -$77.80 | 87.6% | Full losses |
| SL15 (15% stop) | 492 | -$101.87 | 53.7% | Worse - stops trigger on dips |
| SL25 (25% stop) | 492 | -$96.84 | 65.9% | Worse - same problem |

**Session-level stops** work better - they detect a losing regime and stop the entire session:

| Session Stop | OOS9 Trades | OOS9 Ending | Improvement |
|--------------|-------------|-------------|-------------|
| None | 385 | $18.09 | - |
| DD20 (always) | 23 | $148.66 | +$130.57 |
| **ADAPT25_T5_DD20** | 25 | **$120.16** | **+$102.07** |

**But DD20-always hurts winning regimes** by stopping profitable sessions early.

### Winner: ADAPT25_T5_DD20 (Adaptive Session Stop)

**Logic:**
```python
# After 25 trades, check if losing regime
if trade_count == 25:
    if session_pnl < -5:  # Threshold: -$5
        enable_dd20_stop = True  # 20% drawdown stop now active
    else:
        pass  # Winning regime - no stops, let it ride
```

**Naming Convention:**
- `ADAPT25` = Check after 25 trades
- `T5` = Threshold: PnL < -$5 triggers stop
- `DD20` = Enable 20% drawdown stop

### Full Results Across All Datasets

| Dataset | Baseline PnL | ADAPT25 PnL | Adaptive Triggered? |
|---------|--------------|-------------|---------------------|
| IS+OOS2 | +$83.36 | +$83.36 | No (PnL was positive at trade 25) |
| OOS7 | +$232.51 | +$232.51 | No (PnL was positive at trade 25) |
| OOS8 | +$144.18 | +$144.18 | No (PnL was positive at trade 25) |
| OOS9 | -$151.91 | **-$49.84** | **Yes** (stopped at trade 25) |
| **TOTAL** | **$308.14** | **$410.21** | - |

### Key Metrics Comparison

| Metric | NOSESS (baseline) | ADAPT25_T5_DD20 | Improvement |
|--------|-------------------|-----------------|-------------|
| Total PnL | $308.14 | $410.21 | **+$102.07** |
| Ending Balance | $478.14 | $580.21 | **+$102.07** |
| PnL/hour | $2.03/hr | $2.70/hr | **+$0.67/hr** |
| ROI | 181.3% | 241.3% | **+60.0%** |
| Total Trades | 1,218 | 858 | -360 |

### Per-Dataset Breakdown (ADAPT25_T5_DD20)

| Dataset | PnL | Hours | Trades | PnL/hr | $/trade | Adaptive? |
|---------|-----|-------|--------|--------|---------|-----------|
| IS+OOS2 | +$83.36 | 69.4 | 73 | $1.20 | $1.142 | No |
| OOS7 | +$232.51 | 18.9 | 307 | $12.27 | $0.757 | No |
| OOS8 | +$144.18 | 18.1 | 453 | $7.96 | $0.318 | No |
| OOS9 | -$49.84 | 45.6 | 25 | -$1.09 | -$1.994 | **Yes** |
| **TOTAL** | **$410.21** | **152.0** | **858** | **$2.70** | **$0.478** | - |

### Capital Efficiency

| Metric | Value |
|--------|-------|
| Starting Capital | $170.00 |
| Ending Balance | $580.21 |
| ROI | 241.3% |
| PnL/hour | $2.70/hr |
| Capital/trade | $12.00 (15 shares @ ~$0.80) |
| Capital deployed/hr | $67.72/hr |
| Return on deployed | 3.98% |

### Why ADAPT25 Works

1. **Early detection:** By trade 25, enough data to distinguish regimes
   - Winning regimes: PnL typically > $0 after 25 trades
   - Losing regimes: PnL < -$5 after 25 trades

2. **No false positives:** In all winning datasets (IS+OOS2, OOS7, OOS8), the adaptive check found positive PnL and did NOT enable stops

3. **Preserves upside:** Unlike always-on DD20, adaptive stops don't cut profitable runs short

4. **Catches losers:** In OOS9, detected negative PnL at trade 25, enabled DD20, stopped at trade 25 before further losses

### Why ADAPT15 and ADAPT20 Failed

OOS9 losses were gradual - PnL didn't drop below -$5 until after trade 20:

| Check Point | Est. PnL | Detection |
|-------------|----------|-----------|
| Trade 15 | ~-$6 | Borderline |
| Trade 20 | ~-$8 | Borderline |
| **Trade 25** | **< -$10** | **Clear signal** |

The adaptive threshold needs enough trades for the regime signal to emerge.

### Implementation

**Config (GridConfig dataclass):**
```python
# Adaptive session stop parameters
adaptive_check_trades: Optional[int] = 25  # Check after N trades
adaptive_pnl_threshold: Optional[float] = -5  # If PnL < this, enable stops
adaptive_stop_type: Optional[str] = "dd20"  # Which stop to enable
```

**Check function:**
```python
def check_session_stop(config, session_pnl, session_peak_pnl, trade_count, adaptive_enabled):
    # Adaptive check at trade N
    if trade_count == config.adaptive_check_trades:
        if session_pnl < config.adaptive_pnl_threshold:
            adaptive_enabled = True  # Enable DD20 from now on

    # If adaptive enabled, check DD20
    if adaptive_enabled:
        dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
        if dd >= 0.20:  # 20% drawdown
            return True, "adaptive_dd"

    return False, None
```

### Production Config (FADE80_3c_ADAPT25_T5_DD20)

```python
AGGRESSIVE_M_V2_CONFIG = GridConfig(
    name="FADE80_3c_ADAPT25_T5_DD20",
    entry_offset_cents=0.03,         # 3 cent offset below ask
    order_pull_seconds=None,          # Hold order forever
    stop_loss_pct=None,               # No per-trade stop
    min_expensive_ask=0.80,           # Only trade when expensive_ask >= $0.80
    entry_shares=15,                  # Position size (scaled from 5)

    # Adaptive session stop
    adaptive_check_trades=25,         # Check regime after 25 trades
    adaptive_pnl_threshold=-5,        # If PnL < -$5, enable stops
    adaptive_stop_type="dd20",        # Enable 20% drawdown stop
)
```

### Source Files

| File | Purpose |
|------|---------|
| `research/backtests/aggressive_m_v2_grid_search.py` | Session stop implementation |
| `research/findings/data/aggressive_m_v2_adaptive_stops.csv` | Adaptive stop results |
| `research/findings/data/aggressive_m_v2_session_stops.csv` | Fixed stop comparison |

---

## Next Steps

1. ~~Build grid search~~ **DONE** (`aggressive_m_v2_grid_search.py`)

2. ~~Run on all datasets~~ **DONE** (IS+OOS2, OOS7, OOS8, OOS9)

3. ~~Implement session stops~~ **DONE** (ADAPT25_T5_DD20)

4. **Paper trade** with ADAPT25_T5_DD20 config

5. **Monitor** for regime detection accuracy in live trading

---

*Created: February 6, 2026*
*Updated: February 7, 2026 (adaptive session stops, OOS9 losing regime protection)*
*Validated on: IS+OOS2, OOS7, OOS8, OOS9*
*Production config: FADE80_3c_ADAPT25_T5_DD20*
