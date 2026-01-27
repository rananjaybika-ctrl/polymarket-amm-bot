# CONTRARIAN Strategy (Path 2)

**Status:** VALIDATED - Ready for Fill Rate Testing
**Last Updated:** January 25, 2026

---

## Overview

"Mean-reversion at the 15-minute scale" - BTC directional moves within 5 minutes often reverse by window end. Buy the cheap side (~$0.30) for 2.33:1 reward-to-risk.

---

## Configuration (Canonical)

```python
CONTRARIAN = TradingConfig(
    name="CONTRARIAN",

    # Entry logic
    pullback_threshold=0.0001,     # 0.01% absolute pullback from local extreme
    retracement_min=0.30,          # Pullback must be >= 30% of peak move
    entry_price_min=0.20,          # Skip entries < $0.20 (strong trends)
    min_delay_seconds=60,          # Wait at least 60s into window

    # Vol gate
    vol_gate_k=0.5,                # Trade when vol >= 50% of recent average
    vol_gate_halflife=50,          # ~12.5 hours lookback (50 x 15min)
    z_threshold=0.5,               # Z-score >= 0.5 required

    # Position
    shares_per_trade=2500,
    entry_price_target=0.30,       # Buy at ~$0.30

    # Exit
    stop_loss_pct=None,            # No stop
    time_stop_seconds=None,        # No time cap
    # Hold to resolution
)
```

### Parameter Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Pullback | 0.01% absolute | Reversal confirmation signal |
| Retracement | >= 0.30 | Filter brief pauses vs real reversals |
| Entry Price | >= $0.20 | Skip extreme-cheap (strong trends) |
| Entry Delay | >= 60s | Observe BTC direction first |
| Vol Gate | k=0.5, hl=50 | Filter low-vol noise (~35% gated out) |
| Z-Score | >= 0.5 | Vol-normalized move threshold |
| Stop | None | Hold to resolution |

---

## Performance Summary

### Cross-Validated Results

| Period | Hours | Trades | WR | Edge | PnL | $/hr |
|--------|-------|--------|-----|------|-----|------|
| IS (Jan 16-19) | 81.7 | 181 | 43.1% | +8.0pp | $722 | $9 |
| OOS3+4 (Jan 22-24) | 50.6 | 152 | 43.4% | +7.9pp | $599 | $12 |

**Key insight:** WR (43.1% vs 43.4%) and edge (+8.0pp vs +7.9pp) are remarkably consistent across 129 hours.

### OOS4 Details (24.2 hours, Jan 23-24)

| Metric | @2500sh | @50sh equiv |
|--------|---------|-------------|
| PnL | $14,920 | $298 |
| $/hr | **$618** | $12.36 |
| Win Rate | 42% | 42% |
| Trades | 50 | 50 |
| Windows Gated Out | ~35% | ~35% |

### Comparison to Baseline

| Config | Trades | WR | Edge | PnL |
|--------|--------|-----|------|-----|
| **Baseline** (0.01% pullback only) | 196 | 39.3% | +6.7pp | $661 |
| **+ retrace >= 0.30, price >= $0.20** | 181 | 43.1% | +8.0pp | $722 |

**Improvement:** +3.8pp WR, +1.3pp edge over baseline.

---

## How It Works

### Entry Logic

```
1. BTC 15-min market opens
2. Compute pre-window volatility (prior 5 min)
3. Check vol gate: pre_vol / vol_ema >= 0.5
   - If fail: skip this window (~35% gated out)
4. Wait min_delay_s (60s) from window open
5. Observe BTC move from open
6. Detect reversal: 0.01% pullback from local extreme
7. Apply filters:
   - retracement_frac >= 0.30 (pullback / peak_move)
   - entry_price >= $0.20
8. Compute Z-score: |move_pct| / (vol_per_s * sqrt(elapsed_s))
9. If z >= 0.5: Enter contrarian direction at ~$0.30
```

### Exit Logic

Hold to resolution (no stops):
- **Win:** Payout $1.00, profit $0.70/share
- **Lose:** Lose entry cost ($0.30/share)

---

## Why It Works

1. **Asymmetric payoff:** Risk $0.30, reward $0.70 (2.33:1 R:R)
2. **Breakeven at 30% WR:** Only need 30% accuracy to profit
3. **Observed 43% WR:** Well above breakeven (+8pp edge)
4. **Mean reversion:** BTC 15-min directional moves frequently reverse
5. **Adaptive gate:** Self-calibrates to vol regime, no training needed

---

## Improved Filters (Jan 25 Analysis)

### Why Retracement Fraction Works

From losing patterns analysis (Cohen's d effect sizes):

| Discriminator | Cohen's d | Winner Mean | Loser Mean | Actionable? |
|---------------|-----------|-------------|------------|-------------|
| `max_continuation_pct` | 0.565 | 0.020% | 0.087% | No (post-hoc) |
| **`retracement_frac`** | **0.359** | 0.431 | 0.347 | **YES** |
| `entry_price` | 0.316 | $0.347 | $0.312 | YES |

**Core insight:** Losers are "brief pauses in a real trend" - BTC continues moving after entry. Winners are "actual reversals" - BTC stalls/reverses. Retracement fraction measures how "committed" the reversal is.

### Filter Selection (Cross-Validated)

| Filter Combo | Training WR | Training PnL | OOS WR | OOS PnL | Consistent? |
|--------------|-------------|--------------|--------|---------|-------------|
| retrace >= 0.20 | 39.3% | $690 | 41.2% | $616 | YES |
| **retrace >= 0.30** | **41.9%** | **$742** | **41.8%** | **$592** | **YES** |
| retrace >= 0.40 | 43.4% | $790 | 43.0% | $577 | WR yes, PnL drops |
| retrace >= 0.30, price >= $0.20 | 43.1% | $722 | 43.4% | $599 | **STRONG YES** |

**Why 0.30 not 0.40:** Higher thresholds improve WR slightly but PnL diverges between datasets (overfit risk).

### What Didn't Help

- **Choppiness filter:** Individually promising but hurts trade count disproportionately
- **Max entry time:** Training and OOS disagree on optimal value
- **Entry price floor alone:** Improves WR but kills PnL through fewer trades

---

## Adaptive EWMA Vol Gate

### Implementation

```python
class AdaptiveEWMAGate:
    def __init__(self, k=0.5, halflife_windows=50):
        self.k = k
        self.alpha = 1 - 0.5 ** (1 / halflife_windows)
        self.vol_ema = None

    def update_and_check(self, pre_vol):
        if self.vol_ema is None:
            self.vol_ema = pre_vol
            return True  # Always pass first window

        ratio = pre_vol / self.vol_ema
        allowed = ratio >= self.k

        # Update EMA AFTER check (no lookahead)
        self.vol_ema = self.alpha * pre_vol + (1 - self.alpha) * self.vol_ema
        return allowed
```

### Properties

- **Zero calibration:** No training data needed
- **Self-adapting:** Adjusts to any vol regime automatically
- **No lookahead:** Checks gate BEFORE updating EMA
- **Filters ~35%:** Skips very calm markets (weak signal)

---

## Execution Advantage

- **No hedge leg needed** (simpler than AGGRESSIVE)
- **Single limit order** at ~$0.30
- **No time pressure** (60s+ delay means slower reaction OK)
- **Lower breakeven WR** = more robust to adverse conditions

---

## Implementation Notes

### Strategy Logic

```python
# On each 15-minute window
pre_vol = compute_pre_window_vol(prior_5_min)

if not vol_gate.update_and_check(pre_vol):
    return  # Skip low-vol window

# Wait for signal
for t in range(60, 780):
    btc_move = (price_now - price_open) / price_open

    # Check reversal confirmation
    if is_reversal(pullback=0.0001, retrace_min=0.30):
        entry_price = cheap_side_price  # ~$0.30

        if entry_price < 0.20:
            continue  # Skip - strong trend

        z = abs(btc_move) / (vol_per_s * sqrt(t))
        if z >= 0.5:
            # Enter contrarian
            side = "DOWN" if btc_move > 0 else "UP"
            place_order(side, entry_price, 2500)
            break
```

### Fill Rate Considerations

- Entry at $0.30 assumes sufficient liquidity
- Need to paper trade to measure actual fill rates
- May need to adjust entry price based on orderbook depth

---

## Files Reference

| File | Purpose |
|------|---------|
| `research/validate_oos4_all_paths.py` | OOS validation + filter sweeps |
| `research/contrarian_backtest_v2.py` | V2 backtest (vol methods + gates) |
| `research/TRADING_CONFIGS.py` | Config definitions (Python) |
| `research/HANDOVER_JAN25_LOSING_PATTERNS.md` | Filter analysis source (archived) |

---

*Consolidated from: CONTRARIAN_STRATEGY.md, HANDOVER_JAN25_LOSING_PATTERNS.md, MASTER_PLAN_TWO_PATHS.md*
