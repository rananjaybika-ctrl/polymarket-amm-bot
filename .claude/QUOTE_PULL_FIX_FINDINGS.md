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

## Polling Frequency Analysis

### Current State
- **Main loop**: `check_interval=0` means loop runs as fast as possible (~1-2 sec with API calls)
- **Quote pull check**: Happens ONCE per loop iteration (inside `_accumulation_trading_cycle`)
- **155ms latency advantage**: Currently underutilized - we're ~10x slower than we could be

### Actual Flow (Current)
```
T=0.0s: Loop iteration starts, quote pull check, Binance z=0.5 (NEUTRAL)
        → No pull needed
T=0.3s: Binance spikes, z → 2.5 (STRONG)
T=0.5s: (still in API calls for loop iteration)
T=0.8s: Our GTC order gets FILLED at bad price
T=1.2s: Loop iteration completes, next one starts
T=1.4s: Quote pull check finally runs, z=2.5
        → Would have pulled, but ORDER ALREADY FILLED!
```

### Solution Options (Ranked by Effectiveness)

#### Option 1: Event-Driven WebSocket (BEST - Requires Work)
- Already have BinanceClient with WebSocket for price feed
- Add callback: When z-score crosses 2.0, IMMEDIATELY cancel pending orders
- Latency: React within 100-200ms of Binance move
- Implementation: Add `on_z_threshold_crossed(callback)` to BinanceClient

#### Option 2: Reduce Check Interval (SIMPLE - Quick Win)
- Change `check_interval` from 0 (which still has ~1-2s loop time) to explicit fast polling
- Add dedicated quote-pull check inside loop, multiple times per iteration
- Trade CPU for speed
- Could check quotes every 200ms while waiting for fills

#### Option 3: Short-Lived Orders (GTT instead of GTC)
- Instead of GTC (Good-Til-Cancelled) orders that sit on book
- Use GTT with 10-15 second expiry
- Order auto-cancels if not filled quickly
- Downside: More order churn, but safer in trending markets

#### Option 4: Lower Z-Score Threshold (Conservative)
- Currently pull on z >= 2.0 (STRONG)
- Could pull on z >= 1.5 (between MILD and STRONG)
- More false positives, but faster reaction
- Trade aggressiveness for safety

### Recommended Approach
1. **Immediate**: OR filter deployed (z-score check added) ✅
2. **Next**: Implement Option 2 - Add high-frequency quote check inside loop
3. **Future**: Option 1 - Full event-driven WebSocket integration

## Files Modified

1. `src/services/trend_detector.py` - Added z-score OR filter to `should_pull_quote()` ✅
2. `scripts/run_paper_bot.py` - Consider polling frequency in trade loop (TODO)

## Status
- [x] Implement OR filter in trend_detector.py
- [x] Deploy to AWS
- [ ] Add high-frequency quote check inside main loop
- [ ] Consider event-driven WebSocket for fastest reaction
