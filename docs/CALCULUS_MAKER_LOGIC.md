# CALCULUS MAKER Trading Logic - Comprehensive Reference

> Generated: January 7, 2026 | Post-Instant Hedge Implementation

---

## Table of Contents
1. [Entry Conditions](#1-entry-conditions)
2. [Pricing Logic](#2-pricing-logic)
3. [Size Calculation](#3-size-calculation)
4. [Sequential Pairing](#4-sequential-pairing)
5. [Instant Hedge via WebSocket](#5-instant-hedge-via-websocket)
6. [Gradual Chase](#6-gradual-chase)
7. [Emergency Hedging](#7-emergency-hedging)
8. [Z-Score/Trend Integration](#8-z-scoretrend-integration)
9. [Quote Pulling](#9-quote-pulling)
10. [Parameters Reference](#10-parameters-reference)
11. [Comparison: CALC vs Fair Value MM](#11-comparison-calc-vs-fair-value-mm)

---

## 1. Entry Conditions

### When Calculus Decides to Trade

**Location:** `src/strategies/calculus_maker.py:202-227`

Uses **exponential decay mispricing threshold**:

```python
threshold = m_min + (m_max - m_min) * exp(-lambda * (900 - time_remaining))

# Default values:
m_min = 0.005    # Late threshold (0.5% edge)
m_max = 0.025    # Early threshold (2.5% edge)
lambda = 0.004   # Decay constant
```

**Threshold at Different Times:**
| Time Left | Threshold | Max Pair Cost |
|-----------|-----------|---------------|
| 15 min | 2.5% | $0.975 |
| 10 min | 1.1% | $0.989 |
| 5 min | 0.7% | $0.993 |
| 1 min | 0.6% | $0.994 |

**Entry Decision:**
```
mispricing = 1.0 - pair_cost
TRADE if: mispricing >= threshold AND pair_cost <= max_pair_cost (0.995)
```

---

## 2. Pricing Logic

### Patient Bid Offset

**Location:** `src/strategies/calculus_maker.py:156-199`

```python
price = best_bid - mispricing_threshold

# Example at 10 min with best_bid=$0.50:
# threshold = 0.011 (1.1%)
# price = $0.50 - $0.04 = $0.46
```

### Standard Mode Fallback

**Location:** `scripts/run_paper_bot.py:209-248`

| Time Left | Offset | Formula |
|-----------|--------|---------|
| >10 min | -$0.03 | `best_bid - 0.03` |
| 5-10 min | -$0.02 | `best_bid - 0.02` |
| 2-5 min | -$0.01 | `best_bid - 0.01` |
| <2 min | 0 | `best_bid` |
| Emergency | taker | `best_ask` |

---

## 3. Size Calculation

### Quadratic Ramp (Small Early, Large Late)

**Location:** `src/strategies/calculus_maker.py:90-154`

```python
size(t) = min_shares + (max_shares - min_shares) * (1 - t/900)^2
# Rounded to multiples of 5 (Polymarket minimum)
```

| Time Left | Size (5-50 range) |
|-----------|-------------------|
| 15 min | 5 shares |
| 10 min | 6 shares |
| 5 min | 9 shares |
| 2 min | 13 shares |
| 0 min | 50 shares |

**Rationale:**
- Small early = test fills, avoid immediate imbalance
- Large late = complete position under time pressure

---

## 4. Sequential Pairing

### Expensive-First Ordering (30/10 Disaster Fix)

**Location:** `scripts/run_paper_bot.py:3631-3840`

**The Problem:**
Parallel ordering caused asymmetric fills (30 UP / 10 DOWN).
VPS cycles run faster than fill propagation.

**The Solution:**
```
1. BALANCED (UP = DOWN):
   ├─ Identify expensive side (higher ask)
   ├─ Place expensive first
   ├─ Track: _pending_expensive_orders[market.slug]
   └─ Wait for position update before hedging

2. EXPENSIVE FILLS:
   ├─ Detect: current_position > position_when_placed
   ├─ Clear tracking
   └─ Place hedge with PROFIT CEILING

3. TIMEOUT (30 seconds):
   ├─ Cancel pending
   └─ Retry next cycle
```

**Tracking Structure:**
```python
_pending_expensive_orders[market.slug] = {
    "side": "UP" or "DOWN",
    "placed_at": timestamp,
    "position_when_placed": int,
    "expected_size": int,
    "cheap_side": opposite_side,
    "cheap_price": float,
    "cheap_size": float,
    "expensive_price": float,  # For profit ceiling
    "market_slug": str,
    "up_token_id": str,
    "down_token_id": str,
}
```

---

## 5. Instant Hedge via WebSocket

### Sub-Second Hedge Trigger

**Location:** `scripts/run_paper_bot.py:4613-4846`

**Flow:**
```
WebSocket Fill (~100ms from Polymarket)
         │
         ▼
on_fill callback detects expensive side fill
         │
         ▼
asyncio.create_task(_instant_hedge_from_ws)
         │
         ▼
Place hedge with PROFIT CEILING (~200ms total)
```

### Profit-Preserving Ceiling

**Formula:**
```python
MIN_PROFIT = 0.005  # Half cent per pair
max_hedge_price = 1.00 - expensive_price - MIN_PROFIT
```

**Example:**
- Expensive @ $0.73 → max hedge = $1.00 - $0.73 - $0.005 = $0.265
- Never chase above $0.265 (preserves $0.005 profit)

### Implementation Locations:
| Location | Purpose |
|----------|---------|
| Line 4769-4783 | Instant hedge ceiling |
| Line 3688-3692 | Forced DOWN hedge ceiling |
| Line 3740-3744 | Forced UP hedge ceiling |
| Line 4061-4081 | Chase ceiling enforcement |

---

## 6. Gradual Chase

### Patient Price Chasing

**Location:** `scripts/run_paper_bot.py:270-366`

**Parameters by Time:**
| Time Left | Wait | Step | Ceiling (Normal) | Ceiling (Hedge) |
|-----------|------|------|------------------|-----------------|
| >10 min | 60s | $0.02 | $0.50 | $0.65 |
| 5-10 min | 30s | $0.02 | $0.55 | $0.70 |
| 2-5 min | 15s | $0.02 | $0.60 | $0.75 |
| <2 min | 10s | $0.03 | $0.65 | $0.75 |

**Key Rules:**
- MAX_CHASE_ITERATIONS = 5
- After 5 iterations → stop chasing, leave order
- Hedge side gets +$0.15 ceiling bonus
- Profit ceiling caps all chase prices

---

## 7. Emergency Hedging

### Trigger Conditions

**Location:** `scripts/run_paper_bot.py:251-268, 2823-2878`

**Thresholds:**
| Time Left | Imbalance Threshold |
|-----------|---------------------|
| >7 min | 10 shares |
| ≤7 min | 5 shares |

**Emergency Cooldown:** 30 seconds between emergency orders

**Emergency Pricing:**
```python
# Emergency forces TAKER pricing
if is_emergency:
    price = best_ask  # Immediate fill, pay taker
```

---

## 8. Z-Score/Trend Integration

### TrendDetector Setup

**Location:** `scripts/run_paper_bot.py:1427-1434`

```python
TrendDetector(
    z_score_mild=1.0,     # MILD state
    z_score_strong=2.0,   # STRONG state
    z_score_extreme=3.0,  # EXTREME state
)
```

### Impact on Trading:

**1. Dynamic Target Reduction** (Line 3249-3257)
- Strong/Extreme → reduce target by 33-50%
- Prevents overexposure in directional moves

**2. Priority Side Selection** (Line 3290-3300)
- Trending UP → buy UP first (it's getting expensive)
- Trending DOWN → buy DOWN first

**3. Trend-Gated Pair Cost** (Line 3302-3328)
- In strong trends, block if hedge at market would exceed max_pair_cost

**4. Quote Pulling** (Line 3852-3876)
- Cancel stale quotes when z-score moves sharply
- Velocity threshold: 15 bps/sec

**5. Post-Pull Stabilization** (Line 3909-3920)
- Wait for z < 1.0 before re-entering

---

## 9. Quote Pulling

### Trend-Aware Cancellation

**Location:** `scripts/run_paper_bot.py:3852-3876`

```python
await self._engine.check_and_pull_stale_quotes(
    market=market,
    trend_detector=self._trend_detector,
    max_age_secs=20.0 if paper else 10.0,
    velocity_threshold_bps=15.0,  # 15 basis points/sec
)
```

### Event-Driven Pull (Real-time)

**Location:** `scripts/run_paper_bot.py:4548-4603`

- Registers callback with BinanceClient
- Fires when z-score crosses STRONG threshold (2.0)
- Reaction time: ~100-200ms vs 1-2s polling

---

## 10. Parameters Reference

### Calculus Maker Parameters
```python
# Entry thresholds
calc_m_min: float = 0.005          # Late: 0.5% edge
calc_m_max: float = 0.025          # Early: 2.5% edge
calc_lambda: float = 0.004         # Decay constant

# Sizing
calc_max_shares: int = 50          # Max order size
calc_min_shares: int = 5           # Min (Polymarket minimum)
calc_max_pair_cost: float = 0.995  # Never exceed

# Safety
hard_max_imbalance: int = 10       # STOP if |UP-DOWN| >= 10
max_daily_loss: float = 10.0       # Stop if loss > $10

# Features
gradual_chase_enabled: bool = True
sequential_ordering_enabled: bool = True
```

### Fair Value MM Parameters
```python
fv_edge: float = 0.02              # 2 cent spread
fv_sensitivity_early: float = 0.10 # 10% at market open
fv_sensitivity_late: float = 0.50  # 50% near resolution
fv_reprice_threshold: float = 0.03 # Reprice if FV moves 3c
```

---

## 11. Comparison: CALC vs Fair Value MM

| Feature | CALC | Fair Value MM |
|---------|------|---------------|
| **Entry Logic** | Pair cost < threshold | Fair value > ask (selective) |
| **Pricing** | best_bid - threshold | fair_value - edge |
| **Size Ramp** | Quadratic (5→50) | Quadratic (5→50) |
| **Sequential Pairing** | ✅ Enabled | ✅ Enabled |
| **Instant Hedge** | ✅ WebSocket | ✅ WebSocket |
| **Profit Ceiling** | ✅ All paths | ✅ All paths |
| **Imbalance Check** | ✅ Enforced | ❌ Skipped (directional) |
| **Gradual Chase** | ✅ | ✅ |
| **Quote Pulling** | ✅ | ✅ |
| **Depth-based Timeout** | ✅ Dynamic | ❌ Fixed 10s |

### Key Difference:
- **CALC:** Orderbook-driven mispricing arbitrage (buys BOTH sides)
- **FV MM:** Information-driven (buys ONLY undervalued sides via Binance signal)

---

## Conflict Check: Post-Instant Hedge

### FIXED Race Condition: Double Hedge Prevention

**Problem Found:**
- WebSocket instant hedge places hedge immediately
- Main cycle ALSO detects fill and places hedge via `pending_trades`
- Could result in 2x hedge orders for same fill!

**Solution Applied (Jan 7, 2026):**
```python
# Main cycle now checks if instant hedge already handled it:
if pending_info.get("hedge_placed"):
    # WebSocket instant hedge already handled - skip
    del self._pending_expensive_orders[pending_key]
    logger.info("UP/DOWN filled, instant hedge already placed via WebSocket. Skipping.")
else:
    # No instant hedge - place via main cycle
    ... place hedge normally ...
```

**Locations Fixed:**
- Line 3670-3676: UP expensive case
- Line 3731-3737: DOWN expensive case

### Verified Safe:
1. ✅ `_pending_expensive_orders` uses `.copy()` to prevent race conditions
2. ✅ Single asyncio event loop (no true concurrency)
3. ✅ Profit ceiling applied in ALL paths (instant, forced, chase)
4. ✅ Sequential pairing clears tracking after hedge placed
5. ✅ **NEW:** Main cycle skips if `hedge_placed = True`

### Remaining Edge Cases:
1. **Duplicate WebSocket fills:** Could spawn multiple instant hedge tasks
   - Mitigation: `hedge_placed` flag set after first placement
   - Risk: Low (WebSocket messages are unique per order)
2. **Paper mode:** WebSocket not connected, falls back to cycle-based detection

---

## File Locations Summary

| Component | File | Lines |
|-----------|------|-------|
| Entry Decision | calculus_maker.py | 202-227 |
| Threshold Calc | calculus_maker.py | 58-87 |
| Size Ramp | calculus_maker.py | 90-154 |
| Sequential Pairing | run_paper_bot.py | 3631-3840 |
| Instant Hedge | run_paper_bot.py | 4741-4846 |
| Profit Ceiling | run_paper_bot.py | 4769-4783 |
| Gradual Chase | run_paper_bot.py | 270-366 |
| Quote Pulling | run_paper_bot.py | 3852-3876 |
| Emergency | run_paper_bot.py | 2823-2878 |
| Trend Integration | run_paper_bot.py | 3245-3350 |
