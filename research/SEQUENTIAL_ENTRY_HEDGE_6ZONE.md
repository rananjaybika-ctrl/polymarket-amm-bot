# Plan: Sequential Entry→Hedge with 6-Zone Offsets

**Date:** January 14, 2026
**Task:** Implement LIMIT ORDER entry/hedge with velocity-based offsets

---

## Model: Sequential Entry Then Hedge

```
PHASE 1: ENTRY (winner side only)
         - Post LIMIT BID on winner side
         - Wait for fill via WebSocket (sub-second detection)

PHASE 2: HEDGE (after entry fills)
         - Calculate hedge_target = pair_target - entry_price
         - Post LIMIT BID on loser side at hedge_target
         - Wait for hedge fill
```

**Key:** We do NOT post both sides simultaneously. Entry first, then hedge.

---

## 6-Zone Offset Scheme

| Zone | Velocity (bps) | Winner Offset | Loser Offset |
|------|---------------|---------------|--------------|
| 1 (neutral) | 0.00 - 0.05 | -0.01 | -0.01 |
| 2 (moderate) | 0.05 - 0.10 | -0.01 | -0.02 |
| 3 (strong) | 0.10 - 0.30 | 0.00 | -0.04 |
| 4 (very_strong) | 0.30 - 0.50 | +0.01 | -0.06 |
| 5 (extreme) | 0.50 - 1.00 | +0.01 | -0.07 |
| 6 (super_strong) | 1.00+ | +0.02 | -0.08 |

**Winner Offset Logic:**
- Low velocity: Conservative (-0.01 below best_bid) = slower fill, better price
- High velocity: Aggressive (+0.01, +0.02 above best_bid) = faster fill

**Loser Offset Logic (for hedge):**
- Higher velocity = more confidence loser price will drop
- More conservative offset (-0.04 to -0.08) = wait for better price

---

## Entry Bid Calculation

```python
entry_bid = best_bid + winner_offset

# Example: very_strong zone (offset = +0.01)
# best_bid = 0.55
# entry_bid = 0.55 + 0.01 = 0.56

# Clamp to stay below ask (maker)
entry_bid = min(entry_bid, best_ask - 0.001)
```

---

## Hedge Target Calculation

After entry fills at `entry_price`:

```python
hedge_target = pair_target - entry_price

# Example: very_strong zone
# pair_target = 0.95
# entry_price = 0.56 (our fill price)
# hedge_target = 0.95 - 0.56 = 0.39
```

**Hedge bid options:**
1. **Absolute:** Post at `hedge_target` directly
2. **Relative:** Post at `loser_best_bid + loser_offset`
3. **Conservative:** Use `min(loser_best_bid + loser_offset, hedge_target)`

**Recommended: Option 1 (Absolute hedge_target)**
- Simpler logic
- Guarantees pair_cost ≤ pair_target
- Loser offset is backup if orderbook moves

---

## Complete Flow Example

**Scenario:** BTC rising, velocity = +0.35 bps (very_strong zone)

**Orderbook:**
- UP: bid=$0.55, ask=$0.56
- DOWN: bid=$0.44, ask=$0.45

**PHASE 1: Entry**
```
winner_side = UP (velocity > 0)
winner_offset = +0.01 (very_strong zone)
entry_bid = 0.55 + 0.01 = 0.56

Post LIMIT at $0.56 for UP
WebSocket: Fill detected at $0.56
entry_price = $0.56
```

**PHASE 2: Hedge**
```
pair_target = 0.95 (very_strong zone)
hedge_target = 0.95 - 0.56 = 0.39

Post LIMIT at $0.39 for DOWN
Wait for DOWN ask to reach $0.39
hedge_price = $0.39
```

**Result:**
```
pair_cost = 0.56 + 0.39 = 0.95
profit = 1.00 - 0.95 = $0.05 per pair
Both fills as MAKER = rebates on both sides
```

---

## Implementation Constants

```python
# 6-Zone configuration with offsets
VELOCITY_ZONES = {
    'neutral': {
        'vel_min': 0.00, 'vel_max': 0.05,
        'pair_target': 0.97,
        'winner_offset': -0.01,
        'loser_offset': -0.01,
    },
    'moderate': {
        'vel_min': 0.05, 'vel_max': 0.10,
        'pair_target': 0.97,
        'winner_offset': -0.01,
        'loser_offset': -0.02,
    },
    'strong': {
        'vel_min': 0.10, 'vel_max': 0.30,
        'pair_target': 0.96,
        'winner_offset': 0.00,
        'loser_offset': -0.04,
    },
    'very_strong': {
        'vel_min': 0.30, 'vel_max': 0.50,
        'pair_target': 0.95,
        'winner_offset': +0.01,
        'loser_offset': -0.06,
    },
    'extreme': {
        'vel_min': 0.50, 'vel_max': 1.00,
        'pair_target': 0.94,
        'winner_offset': +0.01,
        'loser_offset': -0.07,
    },
    'super_strong': {
        'vel_min': 1.00, 'vel_max': 99.0,
        'pair_target': 0.93,
        'winner_offset': +0.02,
        'loser_offset': -0.08,
    },
}
```

---

## New Methods

### `calculate_entry_bid()`

```python
def calculate_entry_bid(self, best_bid: float, best_ask: float,
                        velocity_bps: float) -> float:
    """Calculate LIMIT entry bid for winner side."""
    zone = self.get_velocity_zone_name(velocity_bps)
    winner_offset = VELOCITY_ZONES[zone]['winner_offset']

    entry_bid = best_bid + winner_offset

    # Stay below ask to remain MAKER
    entry_bid = min(entry_bid, best_ask - 0.001)

    return max(0.01, min(0.95, entry_bid))
```

### `calculate_hedge_target()`

```python
def calculate_hedge_target(self, entry_price: float,
                           velocity_bps: float) -> float:
    """Calculate hedge target after entry fills."""
    zone = self.get_velocity_zone_name(velocity_bps)
    pair_target = VELOCITY_ZONES[zone]['pair_target']

    hedge_target = pair_target - entry_price

    return max(0.01, min(0.95, hedge_target))
```

---

## Files to Modify

| File | Change |
|------|--------|
| `src/strategies/spread_capture.py` | Update VELOCITY_ZONES with offsets, add methods |

---

## Verification

1. Run tests: `pytest tests/ -v`
2. Verify entry_bid < best_ask (maker)
3. Verify hedge_target = pair_target - entry_price
4. Test WebSocket fill detection latency

---

## Key Insights from Analysis

### Why LIMIT Orders (Maker) vs ASK (Taker)

| Fill Method | Fee Impact |
|-------------|------------|
| TAKER (buy at ask) | Pay up to 1.56% fee |
| MAKER (limit order) | Get ~1% rebate |
| **Difference** | **2.56% per trade!** |

### Observer Simulation Was Wrong

The observer fills entries at ASK price (taker behavior). For real trading:
- Entry fills at OUR BID price (what we posted)
- We are MAKER providing liquidity
- We get rebates instead of paying fees

### Sequential Model Matches Observer Logic

The observer's entry→hedge flow is correct, just the fill price was wrong:
1. Entry fills when someone sells into our bid
2. Hedge posted immediately after entry fill (via WebSocket)
3. Hedge fills when someone sells into our hedge bid
