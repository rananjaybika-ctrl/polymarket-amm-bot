# Quote Pull Logic Fix - January 7, 2026

## The Bug: Quote Pull Only Uses Velocity, Not Z-Score

### What Happened
- First live trade: Bought 5 DOWN @ $0.43 = $2.15
- Order placed when z=0.63 (neutral/stable)
- Order **filled** when z=2.56 (STRONG uptrend)
- Market resolved UP, lost $1.90

### Root Cause
The `should_pull_quote()` function in `src/services/trend_detector.py` only checks **velocity** (rate of price change), not **z-score** (absolute position vs strike).

```python
# CURRENT CODE - Only velocity check
if side_upper == "DOWN" and signal.direction == TrendDirection.UP:
    return abs(signal.velocity_bps) > velocity_threshold_bps  # ← ONLY VELOCITY!
```

### Why This Failed
After BTC spiked up:
- Z-score jumped to 2.56 (strong trend - should pull DOWN orders)
- But velocity dropped (price stabilized after the spike)
- Low velocity = no pull trigger
- DOWN order sat there and got filled at the worst time

### The Fix: OR Filter

Pull if EITHER condition is true:
1. **Velocity** > 15 bps/sec (rapid movement)
2. **Z-score** > 2.0 AND trending against the order (sustained position)

```python
# FIXED CODE - Velocity OR Z-score
def should_pull_quote(self, side: str, velocity_threshold_bps: float = None, z_threshold: float = 2.0) -> bool:
    signal = self.get_trend_signal()

    if signal.state == TrendState.NEUTRAL:
        return False

    side_upper = side.upper()

    # Z-SCORE CHECK (NEW): Strong trend in wrong direction = immediate pull
    if signal.state in (TrendState.STRONG, TrendState.EXTREME):
        if side_upper == "DOWN" and signal.direction == TrendDirection.UP:
            return True  # z > 2.0 trending UP, pull DOWN immediately
        if side_upper == "UP" and signal.direction == TrendDirection.DOWN:
            return True  # z > 2.0 trending DOWN, pull UP immediately

    # VELOCITY CHECK (existing): Rapid movement = pull
    if side_upper == "DOWN" and signal.direction == TrendDirection.UP:
        return abs(signal.velocity_bps) > velocity_threshold_bps
    if side_upper == "UP" and signal.direction == TrendDirection.DOWN:
        return abs(signal.velocity_bps) > velocity_threshold_bps

    return False
```

## Polling Frequency Concern

**Problem**: Even with OR filter, if we only check once per second, we're too slow.

**Current flow**:
```
T=0.0s: Binance spikes, z → 2.5
T=0.2s: (no check)
T=0.4s: (no check)
T=0.6s: (no check)
T=0.8s: (no check)
T=1.0s: Quote pull check triggers → Cancel order
        But order may have already been filled!
```

**Solutions to explore**:
1. **Event-driven WebSocket**: Subscribe to Binance price stream, trigger pulls on price events
2. **Higher frequency polling**: Check every 100-200ms instead of 1s
3. **Pre-emptive cancellation**: Cancel on z > 1.5 before it becomes STRONG
4. **Conditional orders**: Use GTT (Good-Til-Time) with short expiry instead of GTC

## Files to Modify

1. `src/services/trend_detector.py` - Add z-score check to `should_pull_quote()`
2. `scripts/run_paper_bot.py` - Consider polling frequency in trade loop

## Status
- [ ] Implement OR filter in trend_detector.py
- [ ] Test in paper mode
- [ ] Deploy to AWS
- [ ] Monitor live trades
