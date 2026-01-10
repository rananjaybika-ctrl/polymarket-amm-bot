# Two-Sided + Velocity Pulling Strategy Analysis

**Date:** January 9, 2026
**Simulation Duration:** ~2 hours across multiple 15-minute markets
**Capital:** $170 | **Target Shares:** 15

---

## Executive Summary

**TWO_SIDED mode outperforms EXPENSIVE_FIRST by 43%** in profit rate.

| Metric | Expensive First | Two Sided | Winner |
|--------|-----------------|-----------|--------|
| Profit Rate | $0.00195/min | $0.00277/min | **Two Sided (+43%)** |
| Avg Cycle Time | 10.0 min | 7.7 min | **Two Sided** |
| Avg Pair Cost | $0.9806 | $0.9786 | **Two Sided** |
| Hourly ROI | 0.34% | 0.49% | **Two Sided** |

---

## Strategy Definitions

### EXPENSIVE_FIRST Mode
1. Identify which side is MORE expensive (higher ask price)
2. Place entry order on expensive side at `best_bid - 0.01`
3. Wait for fill
4. Place hedge order on cheap side at `best_bid - 0.02`
5. Wait for fill
6. Cycle complete

**Hypothesis:** Entering the expensive side first captures more spread because it's trending up and will become even more expensive.

**Result:** REJECTED - The slow fill times on trending sides negate any spread advantage.

### TWO_SIDED Mode
1. Place orders on BOTH sides simultaneously at `best_bid - 0.01`
2. Whichever fills first becomes the "entry"
3. Cancel the other side
4. Place hedge order on opposite side at `best_bid - 0.02`
5. Wait for fill
6. Cycle complete

**Hypothesis:** Taking whatever the market gives you is faster and more capital efficient.

**Result:** CONFIRMED - 43% higher profit rate due to faster fills.

---

## Velocity-Based Quote Pulling

Both strategies use velocity-based quote pulling to avoid adverse fills:

```python
# Constants
VELOCITY_PULL_THRESHOLD = 0.05  # bps/sec (~$5 BTC move in 10s)

# Logic: Pull entry while price is getting CHEAPER (wait for bottom)
def should_pull_entry(velocity_bps: float, entry_side: str) -> bool:
    if entry_side == "UP":
        # BTC falling = UP getting CHEAPER = PULL and wait
        return velocity_bps < -VELOCITY_PULL_THRESHOLD
    else:  # DOWN
        # BTC rising = DOWN getting CHEAPER = PULL and wait
        return velocity_bps > VELOCITY_PULL_THRESHOLD

# Logic: Enter when velocity REVERSES (price at bottom, about to get expensive)
def should_enter_now(velocity_bps: float, entry_side: str) -> bool:
    if entry_side == "UP":
        # BTC rising = UP getting expensive = ENTER NOW (at the bottom)
        return velocity_bps > VELOCITY_PULL_THRESHOLD
    else:  # DOWN
        # BTC falling = DOWN getting expensive = ENTER NOW (at the bottom)
        return velocity_bps < -VELOCITY_PULL_THRESHOLD
```

### Price/Velocity Relationship

| Velocity | BTC Movement | UP Price | DOWN Price |
|----------|--------------|----------|------------|
| **Negative** | BTC falling | CHEAP (losing) | **EXPENSIVE** (winning) |
| **Positive** | BTC rising | **EXPENSIVE** (winning) | CHEAP (losing) |

### Entry Pull Truth Table

| Entry Side | Velocity | Action | Reason |
|------------|----------|--------|--------|
| UP | < -0.05 bps | **PULL** | BTC falling, UP getting CHEAP (wait for cheaper) |
| UP | > +0.05 bps | **ENTER** | BTC rising, UP getting expensive (reversal - at bottom) |
| DOWN | > +0.05 bps | **PULL** | BTC rising, DOWN getting CHEAP (wait for cheaper) |
| DOWN | < -0.05 bps | **ENTER** | BTC falling, DOWN getting expensive (reversal - at bottom) |

### Hedge "Let It Ride" Truth Table

| Hedge Side | Velocity | Action | Reason |
|------------|----------|--------|--------|
| DOWN | > +0.05 bps | **WAIT** | BTC rising, DOWN getting cheap (favorable) |
| DOWN | < -0.05 bps | **HEDGE** | BTC falling, DOWN getting expensive (reversal) |
| UP | < -0.05 bps | **WAIT** | BTC falling, UP getting cheap (favorable) |
| UP | > +0.05 bps | **HEDGE** | BTC rising, UP getting expensive (reversal) |

### Simulation Results: Adverse Fills
- Expensive First: **0% adverse fills**
- Two Sided: **0% adverse fills**
- Velocity triggers observed: 1 (working as intended)

---

## Detailed Simulation Data

### Expensive First (18 cycles)
```
Avg pair cost:        $0.9806
Avg profit/cycle:     $0.0194
Total profit:         $0.35
Avg cycle time:       599.9s (10.0 min)
Entry side dist:      UP: 11, DOWN: 7
Avg entry pulls:      3.0 (repricing working)
```

### Two Sided (21 cycles)
```
Avg pair cost:        $0.9786
Avg profit/cycle:     $0.0214
Total profit:         $0.45
Avg cycle time:       463.9s (7.7 min)
Cancelled side dist:  UP: 11, DOWN: 10 (balanced)
Velocity cancels:     1
```

---

## PnL Projections ($170 capital, 15 target shares)

| Timeframe | Expensive First | Two Sided |
|-----------|-----------------|-----------|
| Per 15-min market | $0.15 | $0.21 |
| Per hour (4 markets) | $0.58 | $0.83 |
| Per day (8 hours) | $4.67 | $6.65 |
| Per month (30 days) | $140.03 | $199.56 |

---

## Implementation Notes for Two-Sided + Pulling

### Key Files
- `src/strategies/spread_capture.py` - Main strategy logic
- `src/strategies/calculus_maker.py` - Has `should_pull_entry()` and `should_hedge_now()` methods
- `src/services/trend_detector.py` - Velocity calculation and thresholds
- `scripts/live_trading_simulator.py` - Simulation framework

### Two-Sided Mode Implementation

```python
# In trading loop:
async def two_sided_cycle():
    # 1. Place orders on BOTH sides
    up_order = place_order("UP", up_bid - ENTRY_OFFSET, size)
    down_order = place_order("DOWN", down_bid - ENTRY_OFFSET, size)

    # 2. Wait for either to fill (with velocity pulling)
    while True:
        velocity = trend_detector.get_velocity_bps()

        # Check for fills
        if up_order.filled:
            cancel(down_order)
            entry_side, hedge_side = "UP", "DOWN"
            break
        if down_order.filled:
            cancel(up_order)
            entry_side, hedge_side = "DOWN", "UP"
            break

        # Velocity-based pulling
        if should_pull_entry(velocity, "UP"):
            cancel(up_order)
            up_order = place_order("UP", up_bid - ENTRY_OFFSET, size)  # Reprice
        if should_pull_entry(velocity, "DOWN"):
            cancel(down_order)
            down_order = place_order("DOWN", down_bid - ENTRY_OFFSET, size)  # Reprice

    # 3. Place hedge
    hedge_order = place_order(hedge_side, hedge_bid - HEDGE_OFFSET, size)

    # 4. Wait for hedge fill (with "let it ride" logic)
    # See calculus_maker.should_hedge_now() for velocity reversal detection
```

### Constants
```python
ENTRY_OFFSET = 0.01          # Place at best_bid - 0.01
HEDGE_OFFSET = 0.02          # Place at best_bid - 0.02
VELOCITY_PULL_THRESHOLD = 0.05  # bps/sec
MAX_HEDGE_WAIT_SECS = 120    # Force hedge after 2 min
MIN_TIME_TO_FORCE_HEDGE = 60 # Force hedge if <60s to expiry
```

---

## Why Two-Sided Wins

1. **Faster Fills**: By placing on both sides, you fill on whichever side the market moves toward first. No fighting the trend.

2. **Lower Pair Cost**: You're not paying the "trending premium" by insisting on the expensive side.

3. **More Cycles**: Faster fills = more completed cycles per market = more profit opportunities.

4. **Balanced Exposure**: Cancelled side distribution (11 UP, 10 DOWN) shows you're not biased to one direction.

---

## Bottleneck: Cycle Time

The main limitation is cycle time (7.7 min average). In a 15-minute market:
- Only ~2 cycles complete
- Only ~10 pairs accumulated (vs 15 target)

### To Improve Throughput
1. **Tighter offsets** (bid - 0.005) - Faster fills, slightly worse prices
2. **Hybrid taker** - Use taker for hedge when time is short
3. **Parallel markets** - Run on multiple expiry windows simultaneously
4. **Higher velocity threshold** - Pull less often, fill faster

---

## Files Modified (Z-Score Removal)

As part of this analysis, z-score was removed from the entire codebase in favor of velocity-only:

1. `src/services/trend_detector.py` - Now uses velocity thresholds (0.02, 0.05, 0.10 bps/sec)
2. `src/api/binance_client.py` - Removed calculate_z_score(), added velocity callbacks
3. `src/strategies/spread_capture.py` - Fixed offsets, velocity-based pulling
4. `src/strategies/calculus_maker.py` - Added should_pull_entry(), should_hedge_now()
5. `src/services/live_trading.py` - Updated event_driven_pull() for velocity
6. `scripts/run_paper_bot.py` - All z-score references removed
7. `scripts/live_trading_simulator.py` - Velocity-only CSV output
8. `tests/test_spread_capture.py` - Updated for new interface

---

## Next Steps to Implement

1. [ ] Add `--mode two_sided` flag to run_paper_bot.py
2. [ ] Implement simultaneous order placement in LiveTradingEngine
3. [ ] Add cancel-other-side logic on fill
4. [ ] Test with real Polymarket WebSocket for fill detection latency
5. [ ] Backtest with historical orderbook data
