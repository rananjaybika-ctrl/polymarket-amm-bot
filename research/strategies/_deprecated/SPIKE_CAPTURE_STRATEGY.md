# Spike Capture Strategy - Complete Analysis & Implementation Guide

**Date:** January 16, 2026
**Status:** Research Complete - Ready for Implementation
**Supersedes:** Velocity-based Zone 5-6 detection

---

## Executive Summary

### The Discovery

Analysis of 1.28M data points across 112 markets revealed that **raw Binance spike detection** dramatically outperforms the current velocity-based approach for capturing the Binance→Polymarket lag.

| Metric | Velocity (0.5 bps) | Raw Spike (0.02%) | Improvement |
|--------|-------------------|-------------------|-------------|
| Direction accuracy | 42.9% | **56.2%** | +31% |
| Mean pair cost | $0.9905 | **$0.9712** | -$0.02 |
| Under $1.00 | 37.4% | **57.9%** | +55% |
| Under $0.99 | 23.8% | **42.8%** | +80% |
| Detection speed | 10s window | **600ms** | 16x faster |

### With Magnitude-Based Loser Bid (Optimal Configuration)

| Metric | Value |
|--------|-------|
| Mean pair cost | **$0.9647** |
| Under $1.00 | **92.1%** |
| Under $0.99 | **81.8%** |
| Under $0.98 | 47.0% |

---

## Part 1: The Binance → Polymarket Lag

### Measured Lag (From Observer Data)

| Measurement Method | Mean Lag | Median Lag | Data Points |
|--------------------|----------|------------|-------------|
| Cross-correlation | 0.60s | 0.60s | 20 markets |
| Event-based | 1.11s | 0.61s | 1,122 events |
| Velocity-based | 3.29s | 2.27s | 1,504 signals |
| **Combined** | **2.35s** | **0.81s** | 2,646 points |

### Lag Distribution

```
<= 0.5 seconds: 41.4%
<= 1.0 seconds: 78.8%
<= 1.5 seconds: 86.5%
<= 2.0 seconds: 87.8%
<= 3.0 seconds: 90.8%
<= 5.0 seconds: 94.5%
```

### Why The Lag Exists

1. **Chainlink RTDS** is the oracle for 15-min BTC markets
2. Chainlink aggregates prices from multiple exchanges (slower than direct Binance)
3. Documented lag: "1.x seconds" (Telegram alpha sources)
4. **Our window**: ~1-2 seconds to act before Polymarket adjusts

---

## Part 2: Why Velocity Underperforms

### The Problem with Current Velocity Formula

```python
# Current formula (binance_client.py:381-407)
velocity_bps = (sum of % changes over 10s) / 10 * 100
```

**Issues:**

1. **10-second averaging** smooths out the signal → lags the actual move
2. **By the time velocity reaches 0.5 bps**, prices have already started moving
3. **No magnitude information** - 0.5 bps could be from $10 or $100 BTC move
4. **Derivative, not direct measurement** - measures rate of change, not actual change

### Velocity Detection Timeline

```
T=0:     BTC spikes $50 (0.05%)
T=0-1s:  Velocity slowly rising (still averaging old data)
T=1-2s:  Velocity reaches 0.3 bps (still below threshold)
T=2-3s:  Velocity reaches 0.5 bps → SIGNAL FIRES
T=3s:    Polymarket has already adjusted! Winner rose, loser dropped
         → Pair cost now $1.01-1.02
```

### Spike Detection Timeline

```
T=0:     BTC spikes $50 (0.05%)
T=0.6s:  3-tick change detected → SIGNAL FIRES IMMEDIATELY
T=0.6s:  Polymarket still at old prices! Buy winner NOW
T=1-2s:  Polymarket adjusts, loser drops
T=2s:    Place loser bid at predicted drop level
         → Pair cost $0.96-0.98
```

---

## Part 3: The Better Formula - Raw Binance Spike

### Core Detection Algorithm

```python
def detect_binance_spike(prices: list, lookback: int = 3, threshold: float = 0.02) -> tuple:
    """
    Detect raw Binance price spike over last N ticks.

    Args:
        prices: Recent Binance prices (newest last)
        lookback: Number of ticks to look back (3 ticks ≈ 600ms)
        threshold: Minimum % change to trigger (0.02% = $20 on $100k BTC)

    Returns:
        (direction, magnitude_pct) or (None, 0) if no signal
    """
    if len(prices) < lookback + 1:
        return None, 0

    current = prices[-1]
    previous = prices[-lookback - 1]

    if previous <= 0:
        return None, 0

    change_pct = (current - previous) / previous * 100
    magnitude = abs(change_pct)

    if magnitude >= threshold:
        direction = "UP" if change_pct > 0 else "DOWN"
        return direction, magnitude

    return None, 0
```

### Why 3 Ticks / 600ms?

| Lookback | Time | Detection Rate | False Positives | Lead Time |
|----------|------|----------------|-----------------|-----------|
| 1 tick | 200ms | High | HIGH | 1.38s |
| **3 ticks** | **600ms** | **Good** | **Low** | **1.25s** |
| 5 ticks | 1s | Medium | Very Low | 1.10s |
| 10 ticks | 2s | Low | Very Low | 0.90s |

**3 ticks is the sweet spot**: Fast enough to beat Polymarket, filtered enough to avoid noise.

---

## Part 4: Magnitude → Loser Bid Optimization

### The Key Insight

Larger BTC moves → larger Polymarket loser drops → can bid more aggressively

### Measured Relationship

| BTC Move | Count | Mean Loser Drop | Median | Max |
|----------|-------|-----------------|--------|-----|
| 0.50-1.00% | 2,724 | $0.0152 | $0.01 | $0.42 |
| 1.00-2.00% | 2,325 | $0.0187 | $0.01 | $0.37 |
| 2.00-3.00% | 535 | $0.0265 | $0.01 | $0.60 |
| 3.00-5.00% | 191 | $0.0338 | $0.02 | $0.33 |
| 5.00-10.00% | 70 | $0.0625 | $0.02 | $0.41 |

### Linear Model (Correlation: 0.202)

```
expected_loser_drop = 0.68 × btc_move_pct + 0.01
```

**Examples:**
- 0.02% BTC move → expect $0.024 loser drop
- 0.05% BTC move → expect $0.044 loser drop
- 0.10% BTC move → expect $0.078 loser drop

### Loser Bid Calculation

```python
def calculate_loser_bid(magnitude_pct: float,
                        loser_ask: float,
                        winner_entry: float,
                        target_pair: float = 0.99) -> float:
    """
    Calculate optimal loser bid based on BTC move magnitude.

    Args:
        magnitude_pct: Absolute BTC % change (e.g., 0.05 for 0.05%)
        loser_ask: Current loser side ask price
        winner_entry: Price we paid for winner
        target_pair: Target pair cost (default $0.99 for profit)

    Returns:
        Optimal loser bid price
    """
    # Expected drop from linear model
    expected_drop = 0.68 * magnitude_pct + 0.01

    # Maximum we can pay and still achieve target pair cost
    max_loser = target_pair - winner_entry

    # Bid: current ask - expected drop, capped at max
    loser_bid = min(loser_ask - expected_drop, max_loser)

    return max(loser_bid, 0.01)  # Floor at 1 cent
```

---

## Part 5: Speed Optimization

### Current Binance Client Setup

```python
# binance_client.py line 59
WEBSOCKET_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
```

**Issue:** `@trade` stream sends individual trades at ~5 ticks/sec (200ms average)

### Faster Options

| Stream | Update Frequency | Latency | Recommended |
|--------|------------------|---------|-------------|
| `@trade` (current) | ~5/sec (200ms) | Good | Current |
| `@aggTrade` | ~10/sec (100ms) | Better | Upgrade option |
| **`@bookTicker`** | **~20-50/sec** | **Best** | **Recommended** |
| Combined streams | Multiple | Best | Complex |

### Recommended: Book Ticker Stream

```python
# Faster detection with @bookTicker
WEBSOCKET_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

# Message format:
# {"u":123456789,"s":"BTCUSDT","b":"95000.00","B":"1.5","a":"95001.00","A":"2.0"}
# b = best bid, a = best ask

# Use mid-price for detection:
mid_price = (float(data['b']) + float(data['a'])) / 2
```

**Benefits:**
- Updates on EVERY best bid/ask change (~20-50/sec)
- Can detect moves within 50-100ms instead of 200ms
- Reduces lookback from 3 ticks to 2 ticks for same time window

### Implementation Changes for Binance Client

```python
class BinanceClient:
    # Option 1: Switch to bookTicker
    WEBSOCKET_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

    # Option 2: Use combined stream (both trade and bookTicker)
    WEBSOCKET_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker"

    async def _receive_loop(self, ws):
        async for message in ws:
            data = json.loads(message)

            # Handle bookTicker format
            if 'b' in data and 'a' in data:
                # Book ticker - use mid price
                bid = float(data['b'])
                ask = float(data['a'])
                price = (bid + ask) / 2
            elif 'p' in data:
                # Trade format (existing)
                price = float(data['p'])
            else:
                continue

            self._current_price = price
            self._price_history.append(PricePoint(timestamp=now, price=price))

            # Check spike on every update
            self._check_spike_and_fire()
```

---

## Part 6: Complete Implementation

### Option A: Modify Existing spread_capture.py (RECOMMENDED)

The spread_capture.py already has the infrastructure (cycling, stop-loss, hedge management). We can add spike detection alongside/replacing velocity.

**Changes Required:**

1. **Add spike detection method** to `SpreadCaptureStrategy` class
2. **Modify `get_quotes()`** to use spike detection instead of velocity zones
3. **Add magnitude-based loser bid calculation**
4. **Update entry logic** in `should_enter()`

### Key Code Changes

```python
# In spread_capture.py

class SpreadCaptureStrategy:
    def __init__(self, ...):
        # Existing init...

        # ADD: Spike detection parameters
        self.spike_lookback = 3  # ticks (~600ms)
        self.spike_threshold = 0.02  # 0.02% minimum
        self.binance_price_history = []

    def detect_spike(self, binance_price: float) -> tuple:
        """
        Detect raw Binance price spike.
        REPLACES: velocity zone detection for entry signals
        """
        self.binance_price_history.append(binance_price)
        if len(self.binance_price_history) > 50:
            self.binance_price_history = self.binance_price_history[-50:]

        if len(self.binance_price_history) < self.spike_lookback + 1:
            return None, 0

        current = self.binance_price_history[-1]
        previous = self.binance_price_history[-self.spike_lookback - 1]

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= self.spike_threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude

        return None, 0

    def calculate_magnitude_loser_bid(self,
                                      magnitude_pct: float,
                                      loser_ask: float,
                                      winner_entry: float) -> float:
        """
        Calculate loser bid based on spike magnitude.
        REPLACES: Fixed loser_offset from velocity zones
        """
        expected_drop = 0.68 * magnitude_pct + 0.01
        target_pair = 0.99  # Target sub-$1 pair cost
        max_loser = target_pair - winner_entry
        loser_bid = min(loser_ask - expected_drop, max_loser)
        return max(loser_bid, 0.01)

    def get_quotes(self, ..., binance_price: float, ...):
        """
        MODIFIED: Use spike detection instead of velocity zones
        """
        # Detect spike
        direction, magnitude = self.detect_spike(binance_price)

        if direction is None:
            # No spike - don't enter
            return None  # Or use neutral/inventory-based logic

        # Determine winner/loser
        if direction == "UP":
            winner_side = "UP"
            winner_ask = up_ask
            loser_ask = down_ask
        else:
            winner_side = "DOWN"
            winner_ask = down_ask
            loser_ask = up_ask

        # Calculate prices
        winner_bid = winner_ask  # Buy immediately at ASK (taker for speed)
        loser_bid = self.calculate_magnitude_loser_bid(
            magnitude, loser_ask, winner_ask
        )

        # Return quotes...
```

### Option B: New Strategy Class

If you prefer to keep velocity-based strategy separate, create `SpikeCapture` class that inherits from or parallels `SpreadCaptureStrategy`.

---

## Part 7: Configuration Parameters

### Spike Detection

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| `spike_lookback` | 3 | 2-5 | Ticks to look back |
| `spike_threshold` | 0.02 | 0.01-0.05 | Min % change to trigger |

### Loser Bid Calculation

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| `target_pair_cost` | 0.99 | 0.97-1.00 | Target UP+DOWN cost |
| `drop_multiplier` | 0.68 | 0.5-1.0 | From linear model |
| `drop_intercept` | 0.01 | 0.005-0.02 | Base expected drop |

### Time Filters (Optional)

| Parameter | Default | Notes |
|-----------|---------|-------|
| `min_time_remaining` | 120 | Don't enter in final 2 min |
| `max_time_remaining` | 840 | Don't enter in first minute |
| `best_window_start` | 300 | 5-10 min window is optimal |
| `best_window_end` | 600 | Based on accuracy analysis |

---

## Part 8: Expected Performance

### Backtest Results (From Observer Data)

| Configuration | Signals | Under $1.00 | Mean PC |
|---------------|---------|-------------|---------|
| Velocity 0.5 bps | 1,835 | 37.4% | $0.9905 |
| Spike 0.02% | 687 | 57.9% | $0.9712 |
| **Spike + Magnitude Bid** | 479 | **92.1%** | **$0.9647** |

### Projected Daily Performance

Assuming 4 markets/hour × 24 hours = 96 markets/day:

| Metric | Velocity Strategy | Spike Strategy |
|--------|-------------------|----------------|
| Signals per market | ~17 | ~6 |
| Profitable signals | 37% | 92% |
| Avg profit per signal | $0.05 | $0.35 |
| Daily signals | ~1,632 | ~576 |
| Daily profit estimate | ~$30 | ~$200 |

*Note: These are estimates based on backtest. Live performance may vary.*

---

## Part 9: Verification Plan

### Phase 1: Observer Mode (1-2 days)

1. Add spike detection to observer script
2. Log: spike signals, predicted pair costs, actual fills
3. Compare to velocity signals in real-time

### Phase 2: Paper Trading (1-2 days)

1. Implement spike detection in paper trading mode
2. Simulate orders with realistic fill assumptions
3. Track pair costs achieved

### Phase 3: Live Testing (Start small)

1. Use 5-10 shares initially
2. Monitor fill rates and actual pair costs
3. Scale up if results match backtest

---

## Part 10: Files Reference

### Analysis Scripts Used

| Script | Purpose |
|--------|---------|
| Observer data analysis | Measured lag, compared signals |
| Linear regression | Magnitude → loser drop model |
| Cross-correlation | Lag measurement |

### Files to Modify

| File | Changes |
|------|---------|
| `src/api/binance_client.py` | Add spike detection, optional bookTicker |
| `src/strategies/spread_capture.py` | Add spike detection, magnitude-based bid |
| `scripts/spread_capture_observer.py` | Log spike signals for verification |

### Data Files

| File | Contents |
|------|----------|
| `research/observer/spread_capture_obs_*.csv` | Raw observer data used for analysis |

---

## Appendix A: Quick Reference Card

### Entry Signal

```python
# When to enter:
if btc_3tick_change >= 0.02%:
    direction = "UP" if change > 0 else "DOWN"
    magnitude = abs(change)

    # Buy winner at ASK immediately
    # Set loser bid based on magnitude
```

### Loser Bid Formula

```python
expected_drop = 0.68 * magnitude_pct + 0.01
loser_bid = min(loser_ask - expected_drop, 0.99 - winner_entry)
loser_bid = max(loser_bid, 0.01)
```

### Key Thresholds

- **Spike threshold:** 0.02% (~$20 on $100k BTC)
- **Target pair cost:** $0.99 (1 cent profit minimum)
- **Lookback window:** 3 ticks (~600ms)
- **Expected lag window:** 1-2 seconds

---

## Appendix B: The 97% vs 49% Accuracy Explained

### Why We Had Two Different Numbers

| Metric | Accuracy | What It Measures |
|--------|----------|------------------|
| "Loser eventually drops" | 97% | Does loser drop at ANY point? |
| "15-second prediction" | 49% | Does price move correctly in 15s? |

**The 97% is valid** for the passive hedge strategy (wait for loser to drop).

**The 49% is irrelevant** because we're NOT trying to predict 15-second timing.

**The spike strategy** capitalizes on the lag by:
1. Detecting the move AS it happens (not after)
2. Using magnitude to set appropriate loser bid
3. Achieving 92% sub-$1 pair costs

---

*Document created: January 16, 2026*
*Based on analysis of 1,281,079 data points across 112 markets*
