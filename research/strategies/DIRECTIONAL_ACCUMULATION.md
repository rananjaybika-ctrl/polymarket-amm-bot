# Directional Accumulation Strategy

## Overview

**Status:** Backtested (Feb 7, 2026)
**Type:** Directional betting with MAKER execution
**Markets:** Polymarket 15-minute BTC binary outcomes

A synthesis of three proven strategies:
1. **FADE signal** from AGGRESSIVE_M_V2 (94.7% directional accuracy at $0.80)
2. **Calculus sizing** (quadratic ramp: small early, large late)
3. **AS time window** (220-500s optimal entry)

---

## Core Logic

### FADE Signal
When BTC spikes but Polymarket doesn't react, the expensive_side stays expensive because the market doesn't believe the spike. **The market is usually RIGHT.**

- BTC spikes UP → DOWN token should get cheaper → but it stays expensive → buy DOWN
- BTC spikes DOWN → UP token should get cheaper → but it stays expensive → buy UP

### Entry Formula
```python
# Line 518 in directional_accumulation_backtest.py
entry_bid = max(0.01, expensive_ask - config.entry_offset_cents)
```

With default `entry_offset_cents = 0.03`:
- `entry_bid = expensive_ask - 0.03` (MAKER order, 3c below ask)

### Sizing Formula (Calculus Quadratic Ramp)
```python
# Lines 255-272 in directional_accumulation_backtest.py
def get_calculus_size(time_remaining, max_shares=50, min_shares=5):
    t = max(0, min(time_remaining, 900))
    urgency = (1 - t / 900) ** 2  # 0 at t=900, 1 at t=0
    raw_size = min_shares + (max_shares - min_shares) * urgency
    return max(min_shares, 5 * round(raw_size / 5))
```

| Time Remaining | Shares | Rationale |
|----------------|--------|-----------|
| 15 min (900s) | 5 | Small - test fills first |
| 5 min (300s) | 9 | Ramping up |
| 2 min (120s) | 40 | Approaching completion |
| 0 min | 50 | Max - complete position |

---

## Filters

### 1. Expensive Ask Threshold
```python
# Line 292 in directional_accumulation_backtest.py
if expensive_ask < config.min_expensive_ask:  # default: 0.80
    return False
```

| Threshold | FADE Accuracy | Trade Volume |
|-----------|---------------|--------------|
| $0.65 | 78.8% | Higher |
| $0.80 | 95.9% | Lower |

**Recommendation:** Use $0.80 for accuracy, $0.65 for volume.

### 2. Time Window (AS Optimal)
```python
# Lines 300-303 in directional_accumulation_backtest.py
if time_remaining < config.entry_window_min_secs:  # default: 90s
    return False
if time_remaining > config.entry_window_max_secs:  # default: 900s
    return False
```

- **220-500s:** AS optimal window (best fill quality)
- **90-900s:** Wider window (more trades)

### 3. Velocity Filter (Skip Confirmed DOWN Spikes)
```python
# Lines 311-314 in directional_accumulation_backtest.py
if spike_dir == 'DOWN' and velocity_bps < 0:
    return False, "confirmed DOWN spike (vel<0)"
```

When spike=DOWN AND velocity<0, BTC is falling and spike confirms it. Skip these - they're not FADE opportunities.

### 4. Volatility Gate (EWMA Z-Score)
```python
# Lines 306-309 in directional_accumulation_backtest.py
if z_score < config.z_lo:  # default: 0.0
    return False
if z_score > config.z_hi:  # default: 1.5
    return False
```

| Z-Score | Regime | Action |
|---------|--------|--------|
| z < 0 | LOW | Skip - insufficient movement |
| 0 ≤ z < 1.5 | MEDIUM | Trade - optimal |
| z ≥ 1.5 | HIGH/EXTREME | Skip - accuracy drops |

---

## MAKER Execution

**Critical:** This strategy uses MAKER orders (0% fees), NOT TAKER (2% fees).

### Fill Logic
```python
# Lines 400-413 in directional_accumulation_backtest.py
# Check MAKER fill: ask drops to our bid
if entry_side == "UP":
    entry_ask = up_ask
else:
    entry_ask = down_ask

if pd.notna(entry_ask) and entry_ask <= entry_bid:
    # FILLED! Move to open_positions
    order['entry_fill_price'] = entry_bid  # MAKER fills at our price
```

### Iteration Requirement
**Mistake #51 Prevention:** Must iterate ALL observer rows for MAKER fills, not just spike rows.

---

## Exit Strategy

### Hold to Resolution (Default)
```python
# Lines 537-565 in directional_accumulation_backtest.py
# At resolution:
if resolution == entry_side:
    pnl_gross = (1.0 - entry_fill_price) * entry_shares  # Win
else:
    pnl_gross = (0.0 - entry_fill_price) * entry_shares  # Lose
```

- Winner pays $1.00 per share
- Loser pays $0.00 per share
- NO stop loss (stops exit winners at 80-88% accuracy)
- NO time-stop sell
- NO merge (pair costs > $1.00)

### Optional Stop Loss
```python
# Lines 419-463 in directional_accumulation_backtest.py
if config.stop_loss_pct is not None:
    drop_pct = (entry_fill_price - current_bid) / entry_fill_price
    if drop_pct >= config.stop_loss_pct:
        # Stop-loss triggered - exit at bid (TAKER)
```

**Not recommended** - stops exit winners.

---

## Backtest Results

### Configuration Used
```
min_expensive_ask: 0.80
entry_offset_cents: 0.03
entry_window: 90-900s
z_score: disabled (-10 to 10)
stop_loss: None (hold to resolution)
```

### Results by Dataset (Feb 7, 2026)

| Dataset | Trades | Total PnL | $/Hour | FADE Accuracy | Sharpe | ROI % |
|---------|--------|-----------|--------|---------------|--------|-------|
| IS+OOS2 | 73 | $78.32 | $1.13 | 95.9% | 15.27 | 46.1% |
| OOS7 | 307 | $313.29 | $16.53 | 93.5% | 15.04 | 184.3% |
| OOS8 | 453 | $186.59 | $10.29 | 89.8% | 5.13 | 109.8% |
| **OOS9** | **385** | **-$216.72** | **-$4.75** | **84.4%** | **-5.46** | **-127.5%** |

### Key Finding: OOS9 Trending Regime

OOS9 (Feb 1-3) is a **trending regime** where:
- Spikes are REAL signals, not noise
- FADE bets against momentum and loses
- Z-score filter should help (0 < z < 1.5)

---

## Configuration Reference

### DirectionalConfig Class
```python
@dataclass
class DirectionalConfig:
    name: str = "DA_DEFAULT"

    # FADE filter thresholds
    min_expensive_ask: float = 0.80  # Optimal accuracy
    min_time_remaining: float = 90.0

    # Time window
    entry_window_min_secs: float = 90.0
    entry_window_max_secs: float = 900.0

    # Volatility gate
    z_lo: float = 0.0
    z_hi: float = 1.5

    # Entry pricing
    entry_offset_cents: float = 0.03  # 3c below ask

    # Sizing
    min_shares: int = 5
    max_shares: int = 50

    # Stop loss
    stop_loss_pct: Optional[float] = None  # Hold to resolution
```

---

## Files

- **Backtest:** `research/backtests/directional_accumulation_backtest.py` (873 lines)
- **Results:** `research/findings/data/directional_accumulation_results.csv`
- **Trades:** `research/findings/data/directional_accumulation_results_trades.csv`
- **Plan:** `/Users/rananjaybika/.claude/plans/glistening-drifting-lantern.md`

---

## Usage

```bash
# Run on all datasets with optimal config
python research/backtests/directional_accumulation_backtest.py \
    --data all \
    --min-expensive-ask 0.80 \
    --entry-offset 0.03 \
    --window-min 90 \
    --window-max 900

# With z-score filtering (for trending regime protection)
python research/backtests/directional_accumulation_backtest.py \
    --data all \
    --min-expensive-ask 0.80 \
    --z-lo 0.0 \
    --z-hi 1.5
```

---

## Known Limitations

1. **OOS9 loses money** - Trending regime defeats FADE signal
2. **Fixed sizing** - Calculus ramp doesn't account for confidence
3. **No loser accumulation** - Feature exists but disabled by default
4. **No hedge** - 100% directional risk

---

## Next Steps

1. **Regime detection** - Add momentum filter to skip trending markets
2. **Confidence-based sizing** - Baguette inverse sizing (smaller on high-confidence)
3. **Live testing** - Paper trade first, then small size live

---

*Created: February 7, 2026*
*Source: FADE + Calculus + AS synthesis*
