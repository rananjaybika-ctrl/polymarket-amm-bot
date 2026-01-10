# Strategy Comparison: Three Trading Approaches

## Overview

We have identified THREE distinct trading strategies for BTC 15-minute markets:

1. **Current Grid Maker Logic** - Gabagool-style two-sided passive market making
2. **Calculus MAKER Logic** - Exponential mispricing threshold
3. **Gabagool TAKER Logic** - Late-entry directional betting

---

## Strategy 1: Current Grid Maker Logic (Existing Bot)

### Implementation
File: `scripts/run_paper_bot.py:196-235`

```python
def get_patient_price(best_bid, best_ask, time_remaining_secs, is_emergency=False):
    if is_emergency:
        return best_ask

    # Fixed timing tiers
    if time_remaining_secs > 600:      # 10-15 min
        return best_bid - 0.03
    elif time_remaining_secs > 300:    # 5-10 min
        return best_bid - 0.02
    elif time_remaining_secs > 120:    # 2-5 min
        return best_bid - 0.01
    else:                               # 0-2 min
        return best_bid
```

### Characteristics
| Aspect | Value |
|--------|-------|
| Order Type | MAKER (post bids) |
| Trading Window | Full 15 minutes |
| Price Logic | Fixed offsets from best_bid |
| Pair Cost Limit | Strict < $1.00 |
| Size | Fixed per config |

### Pros/Cons
- ✅ Simple, predictable
- ✅ Low risk (always profitable if filled)
- ❌ Static, doesn't adapt to market conditions
- ❌ May miss opportunities or overpay

---

## Strategy 2: Calculus MAKER Logic (Phase 2-4 Plan)

### Mathematical Model

**Mispricing Threshold (Exponential Decay):**
```
m(t) = m_min + (m_max - m_min) · e^(-λ(900-t))

where:
  m_min = 0.01  (late market threshold)
  m_max = 0.04  (early market threshold)
  λ = 0.005    (decay constant)
```

**Dynamic Size (Quadratic Ramp):**
```
size(t) = S_min + (S_max - S_min) · (1 - t/900)²

where:
  S_min = 1 share
  S_max = 20 shares
```

### Values at Key Times
| Time Left | m(t) | Max Pair Cost | Size |
|-----------|------|---------------|------|
| 900s | 0.040 | $0.960 | 1 |
| 600s | 0.017 | $0.983 | 3 |
| 300s | 0.012 | $0.988 | 9 |
| 120s | 0.011 | $0.989 | 15 |
| 60s | 0.010 | $0.990 | 18 |
| 0s | 0.010 | $0.990 | 20 |

### Implementation
```python
import math

def get_mispricing_threshold(t: float) -> float:
    t = max(0, min(t, 900))
    m_min, m_max, lam = 0.01, 0.04, 0.005
    return m_min + (m_max - m_min) * math.exp(-lam * (900 - t))

def get_dynamic_size(t: float) -> int:
    t = max(0, min(t, 900))
    urgency = (1 - t / 900) ** 2
    return max(1, int(1 + 19 * urgency))

def should_buy(pair_cost: float, t: float) -> bool:
    threshold = get_mispricing_threshold(t)
    return (1.0 - pair_cost) >= threshold
```

### Characteristics
| Aspect | Value |
|--------|-------|
| Order Type | MAKER (post bids) |
| Trading Window | Full 15 minutes |
| Price Logic | Dynamic offset = m(t) |
| Pair Cost Limit | $0.96 early → $0.99 late |
| Size | 1 early → 20 late |

### Pros/Cons
- ✅ Mathematically principled
- ✅ Adapts to time remaining
- ✅ Strict pair cost control
- ❌ Still MAKER (may not fill)
- ❌ Doesn't use market information

---

## Strategy 3: Gabagool TAKER Logic (Live Analysis)

### Discovery Source
Live monitoring of Gabagool (0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d)
on market btc-updown-15m-1766824200 (Dec 27, 3:30AM ET)

### Mathematical Model

**Price Tolerance (Quadratic Ramp, last 2 min only):**
```
max_price(t) = 0.25 + 0.70 · ((120-t)/120)²   for t ≤ 120
max_price(t) = 0.25                            for t > 120
```

**Size (Quadratic Ramp):**
```
size(t) = 5 + 45 · ((120-t)/120)²   for t ≤ 120
size(t) = 5                          for t > 120
```

### Values at Key Times
| Time Left | Max Price | Size | Action |
|-----------|-----------|------|--------|
| 900s | $0.25 | 5 | WAIT (only extreme mispricing) |
| 300s | $0.25 | 5 | WAIT |
| 120s | $0.25 | 5 | START opportunistic |
| 60s | $0.43 | 16 | Moderate |
| 30s | $0.64 | 30 | Aggressive |
| 10s | $0.84 | 42 | Very aggressive |
| 0s | $0.95 | 50 | Maximum |

### Live Market Observation (btc-updown-15m-1766824200)

**Timing Pattern:**
- ALL 23 trades in last 63 seconds
- 8 burst events, avg 7.7s apart
- Multiple fills per second (latency optimized)

**Three Phases:**
1. Phase 1 (63-59s): Initial UP bet (26 shares @ $0.93-0.94)
2. Phase 2 (25-21s): Massive DOWN hedge (108 shares @ $0.08-0.15)
3. Phase 3 (19-9s): Resume UP with conviction (141 shares @ $0.87-0.95)

**Result:**
- Final position: 167 UP, 108 DOWN
- Pair cost: $1.0389 (ABOVE $1.00!)
- Market outcome: UP WON
- P&L: -$0.23 (small loss despite correct direction)

### Implementation
```python
def should_trade_latency(t: float) -> bool:
    return t <= 120

def get_max_price_latency(t: float) -> float:
    if t > 120:
        return 0.25
    progress = (120 - t) / 120
    return min(0.95, 0.25 + 0.70 * progress ** 2)

def get_size_latency(t: float) -> int:
    if t > 120:
        return 5
    progress = (120 - t) / 120
    return int(5 + 45 * progress ** 2)

def get_direction_bias(btc_change: float) -> str:
    if btc_change > 0.001:
        return "UP"
    elif btc_change < -0.001:
        return "DOWN"
    return "NEUTRAL"
```

### Characteristics
| Aspect | Value |
|--------|-------|
| Order Type | TAKER (sweep asks) |
| Trading Window | Last 2 minutes only |
| Price Logic | Max acceptable ask price |
| Pair Cost Limit | Flexible up to $1.02 |
| Size | 5 early → 50 late |

### Pros/Cons
- ✅ Uses information advantage (BTC direction)
- ✅ Guaranteed fills (TAKER)
- ✅ Minimizes exposure time
- ❌ Higher risk (accepts pair cost > $1.00)
- ❌ Requires latency optimization
- ❌ Directional bet, not pure arbitrage

---

## Side-by-Side Comparison

| Aspect | Grid Maker | Calculus MAKER | Gabagool TAKER |
|--------|------------|----------------|----------------|
| **Order Type** | MAKER | MAKER | TAKER |
| **Window** | Full 15 min | Full 15 min | Last 2 min |
| **Curve** | Step function | Exponential | Quadratic |
| **Max Pair Cost** | < $1.00 | < $1.00 | < $1.02 |
| **Min Size** | Fixed | 1 | 5 |
| **Max Size** | Fixed | 20 | 50 |
| **Philosophy** | Patient | Dynamic patient | Aggressive late |
| **Risk** | Low | Low | Medium |
| **Fill Rate** | Variable | Variable | High |
| **Information** | None | Time only | Time + BTC price |

---

## Hybrid Strategy Recommendation

Combine all three approaches:

```python
def execute_hybrid(t, best_bid, best_ask, pair_cost, btc_change):
    # Phase A: Early market (t > 120s) - Use Calculus MAKER
    if t > 120:
        threshold = get_mispricing_threshold(t)
        if (1.0 - pair_cost) >= threshold:
            price = best_bid - threshold
            size = get_dynamic_size(t)
            return {"type": "MAKER", "price": price, "size": size}
        return {"type": "WAIT"}

    # Phase B: Late market (t <= 120s) - Use Gabagool TAKER
    max_price = get_max_price_latency(t)
    size = get_size_latency(t)
    direction = get_direction_bias(btc_change)

    # Opportunistic cheap buys
    if best_ask < 0.25:
        return {"type": "TAKER", "price": best_ask, "size": size}

    # Aggressive phase (last 30s)
    if t <= 30 and best_ask < max_price:
        return {"type": "TAKER", "price": best_ask, "size": size}

    return {"type": "WAIT"}
```

---

## Historical Performance

### Gabagool Historical (10 markets, 3 days)
| Metric | Value |
|--------|-------|
| Avg Pair Cost | $0.9851 |
| Profitable Markets | 9/10 (90%) |
| Avg Margin | 1.49% |

### Gabagool Live Market
| Metric | Value |
|--------|-------|
| Pair Cost | $1.0389 |
| Result | LOSS (-$0.23) |
| Strategy | Late-entry directional |

---

## Key Formulas Summary

**Calculus MAKER:**
```
m(t) = 0.01 + 0.03 · e^(-0.005(900-t))
size(t) = 1 + 19 · (1 - t/900)²
```

**Gabagool TAKER:**
```
max_price(t) = 0.25 + 0.70 · ((120-t)/120)²
size(t) = 5 + 45 · ((120-t)/120)²
```

---

*Analysis date: December 27, 2025*
*Based on live monitoring of Gabagool wallet and 20+ market historical analysis*
