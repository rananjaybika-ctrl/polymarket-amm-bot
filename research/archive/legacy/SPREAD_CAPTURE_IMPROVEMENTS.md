# Spread Capture Strategy Improvements

**Date:** January 14, 2026
**Based on:** 8-hour AWS observer data analysis + codebase deep dive

---

## Executive Summary

Simulation shows these improvements can increase profit from **$20.92 to $32.70** (+56%):
- Multi-zone dynamic hedge tightening: +$10.80
- No order pulling (counter-intuitive but proven): prevents -$33 loss
- Hourly profit increase: $2.57 → $4.01/hour

---

## Improvement List

### 1. MULTI-ZONE DYNAMIC HEDGE TIGHTENING ⭐ HIGH IMPACT

**Current State (spread_capture.py lines 60-72):**
```python
# Only 3 zones, only adjusts OFFSETS, not hedge targets
VELOCITY_THRESHOLD = 0.05  # Neutral → Moderate
VELOCITY_STRONG = 0.10     # Moderate → Strong
```

**Problem:**
- Only 3 velocity zones (Neutral, Moderate, Strong)
- Hedge target is implicitly fixed at ~0.97 pair cost
- Missing higher velocity zones where we can be MORE aggressive

**Proposed Change:**
```python
# NEW: 6 velocity zones with dynamic hedge targets
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'pair_target': 0.97},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'pair_target': 0.97},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'pair_target': 0.96},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'pair_target': 0.95},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'pair_target': 0.94},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'pair_target': 0.93},
}
```

**Key Logic - Dynamic Tightening:**
```python
def get_dynamic_hedge_target(self, entry_price: float, velocity_bps: float) -> float:
    """Get hedge target based on current velocity zone."""
    abs_vel = abs(velocity_bps)

    # Find current zone
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            pair_target = zone['pair_target']
            break
    else:
        pair_target = 0.93  # Super strong default

    hedge_target = pair_target - entry_price
    return max(0.01, min(0.95, hedge_target))
```

**Critical Rule - Only Tighten, Never Loosen:**
```python
# When velocity strengthens in entry direction, tighten target
if vel_direction == entry_velocity_direction:
    new_target = get_dynamic_hedge_target(entry_price, velocity_bps)
    if new_target < current_hedge_target:
        current_hedge_target = new_target  # Tighten
    # Never loosen - keeps original target if velocity weakens
```

**Simulation Results:**
| Metric | Before | After | Δ |
|--------|--------|-------|---|
| Profit | $20.92 | $32.70 | +$11.78 |
| Avg Pair Cost | $0.956 | $0.932 | -$0.024 |
| Win Rate | 90.6% | 96.9% | +6.3% |

**Files to Modify:**
- `src/strategies/spread_capture.py` - Add VELOCITY_ZONES dict, modify calculate_offsets()
- `scripts/run_paper_bot.py` - Pass velocity to strategy for dynamic updates

---

### 2. NO ORDER PULLING ON VELOCITY FLIP ⭐ HIGH IMPACT

**Current State (spread_capture.py lines 422-477):**
```python
def check_velocity_zone_transition(self, velocity_bps):
    # Returns sides_to_pull when zone changes
    if zone_changed:
        sides_to_pull = ["UP", "DOWN"]  # Pulls both sides
```

**Problem:**
Our simulation proved order pulling DESTROYS performance:

| Pull Threshold | Profit | Merges | Pulled |
|----------------|--------|--------|--------|
| **NO PULL** | **$32.70** | **32** | **0** |
| >= 0.5 bps | $10.52 | 21 | 16 |
| >= 0.3 bps | $3.62 | 17 | 24 |
| Any flip | -$0.82 | 14 | 31 |

**Why Pulling Fails:**
- Velocity flips 160+ times per market (every ~5 seconds)
- Pulling cancels orders that would have filled profitably
- Fixed hedge logic is PATIENT - let it work

**Proposed Change:**
```python
def check_velocity_zone_transition(self, velocity_bps):
    """Check velocity zone but DON'T recommend pulling."""
    # ... zone detection logic ...

    # CHANGE: Never pull on velocity transitions
    # Fixed hedge targets will fill when price reaches target
    sides_to_pull = []  # Empty - never pull

    return (current_zone, zone_changed, sides_to_pull)
```

**Or remove pulling entirely from run_paper_bot.py:**
```python
# BEFORE (lines 4369-4380):
zone, zone_changed, sides_to_pull = strategy.check_velocity_zone_transition(velocity_bps)
if sides_to_pull:
    for side in sides_to_pull:
        await self._engine.cancel_pending_order(key)

# AFTER:
zone, zone_changed, _ = strategy.check_velocity_zone_transition(velocity_bps)
# Don't pull - let fixed hedge targets fill naturally
```

**Files to Modify:**
- `src/strategies/spread_capture.py` - Return empty sides_to_pull
- `scripts/run_paper_bot.py` - Remove pulling logic in _run_spread_capture_cycle

---

### 3. FIXED HEDGE TARGET PATTERN (from Observer)

**Current State:**
The live strategy uses continuous quoting (both sides simultaneously) which is good.
But for any sequential entry→hedge logic, the observer shows a critical pattern.

**Observer Pattern (spread_capture_observer.py lines 166-187):**
```python
@dataclass
class EntryState:
    """Track entry fill and FIXED hedge target per scenario."""
    entry_filled: bool = False
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    hedge_target: float = 0.0  # SET ONCE, NEVER RECALCULATE
    hedge_filled: bool = False
```

**Key Insight:**
Once entry fills, the hedge target is LOCKED. This prevents the "chasing" bug where:
1. Entry fills at $0.52
2. Hedge target = 0.97 - 0.52 = $0.45
3. Price moves, hedge target keeps recalculating lower and lower
4. Order never fills, pair cost explodes

**Application to Live Strategy:**
The current continuous quoting approach avoids this by not using sequential entry→hedge.
But if we add dynamic hedge tightening (Improvement #1), we must ensure:
- Hedge target can only TIGHTEN (lower), never LOOSEN
- Once hedge fills, it's locked

**Implementation Guard:**
```python
# In SpreadCaptureState dataclass, add:
locked_hedge_target: Optional[float] = None
hedge_target_direction: Optional[str] = None  # "UP" or "DOWN"

# When setting hedge target:
def set_hedge_target(self, entry_price: float, entry_side: str, velocity_bps: float):
    if self.locked_hedge_target is None:
        # First time - set it
        self.locked_hedge_target = self.get_dynamic_hedge_target(entry_price, velocity_bps)
        self.hedge_target_direction = "DOWN" if entry_side == "UP" else "UP"
    else:
        # Update only if tightening
        new_target = self.get_dynamic_hedge_target(entry_price, velocity_bps)
        if new_target < self.locked_hedge_target:
            self.locked_hedge_target = new_target
```

---

### 4. CONFIGURABLE VELOCITY PARAMETERS

**Current State (spread_capture.py lines 60-72):**
```python
# HARDCODED constants - cannot tune without code changes
VELOCITY_THRESHOLD = 0.05
VELOCITY_STRONG = 0.10
BASE_OFFSET = 0.02
TIGHT_OFFSET = -0.01
# ... etc
```

**Problem:**
- Parameters are hardcoded, not configurable
- Cannot A/B test different settings
- Observer uses configurable ScenarioParams, live code doesn't

**Observer Pattern (spread_capture_observer.py lines 50-90):**
```python
@dataclass
class ScenarioParams:
    name: str
    velocity_threshold: float = 0.05
    velocity_strong: float = 0.10
    base_offset: float = 0.02
    tight_offset: float = -0.01
    wide_offset: float = 0.02
    very_wide_offset: float = 0.04

SCENARIOS = {
    "default": ScenarioParams(name="default"),
    "conservative": ScenarioParams(name="conservative", velocity_threshold=0.08),
    "aggressive": ScenarioParams(name="aggressive", tight_offset=-0.02),
}
```

**Proposed Change:**
```python
# In spread_capture.py, add:
@dataclass
class SpreadCaptureParams:
    """Configurable parameters for spread capture strategy."""
    # Velocity zone thresholds
    velocity_threshold: float = 0.05
    velocity_strong: float = 0.10
    velocity_very_strong: float = 0.30
    velocity_extreme: float = 0.50
    velocity_super_strong: float = 1.00

    # Hedge targets per zone
    neutral_pair_target: float = 0.97
    moderate_pair_target: float = 0.97
    strong_pair_target: float = 0.96
    very_strong_pair_target: float = 0.95
    extreme_pair_target: float = 0.94
    super_strong_pair_target: float = 0.93

    # Quote offsets
    base_offset: float = 0.02
    tight_offset: float = -0.01
    wide_offset: float = 0.02
    very_wide_offset: float = 0.04

class SpreadCaptureStrategy:
    def __init__(self, params: Optional[SpreadCaptureParams] = None, ...):
        self.params = params or SpreadCaptureParams()
```

**Files to Modify:**
- `src/strategies/spread_capture.py` - Add SpreadCaptureParams dataclass
- `scripts/run_paper_bot.py` - Pass params to strategy constructor
- `config.py` or environment variables for production tuning

---

### 5. ENTRY STATE TRACKING FOR CONTINUOUS QUOTING

**Current Gap:**
The live strategy tracks position (up_shares, down_shares, avg_prices) but doesn't track:
- Which side was the "entry" vs "hedge"
- The original entry velocity direction
- Whether hedge target should tighten based on velocity strengthening

**Current State (SpreadCaptureState lines 141-182):**
```python
@dataclass
class SpreadCaptureState:
    # Position tracking
    up_shares: int = 0
    down_shares: int = 0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0

    # NO: entry direction tracking
    # NO: velocity direction at entry
    # NO: locked hedge target
```

**Proposed Addition:**
```python
@dataclass
class SpreadCaptureState:
    # ... existing fields ...

    # NEW: Entry tracking for dynamic hedge tightening
    entry_side: Optional[str] = None           # "UP" or "DOWN" - first side to fill
    entry_velocity_direction: Optional[str] = None  # Velocity direction at entry
    locked_hedge_target: Optional[float] = None     # Fixed hedge target (only tightens)

    def record_entry(self, side: str, price: float, velocity_bps: float):
        """Record entry and set initial hedge target."""
        if self.entry_side is None:  # First fill is the entry
            self.entry_side = side
            self.entry_velocity_direction = "UP" if velocity_bps > 0 else "DOWN"
            # Initial hedge target based on current velocity zone
            pair_target = get_pair_target_for_velocity(velocity_bps)
            self.locked_hedge_target = pair_target - price

    def maybe_tighten_hedge_target(self, velocity_bps: float, entry_price: float):
        """Tighten hedge target if velocity strengthened in entry direction."""
        if self.locked_hedge_target is None:
            return

        current_direction = "UP" if velocity_bps > 0 else "DOWN"
        if current_direction == self.entry_velocity_direction:
            # Velocity still in entry direction - check for tightening
            new_target = get_pair_target_for_velocity(velocity_bps) - entry_price
            if new_target < self.locked_hedge_target:
                self.locked_hedge_target = new_target
```

**Integration Point (on_fill method):**
```python
def on_fill(self, side: str, price: float, size: int, velocity_bps: float = 0.0):
    """Handle fill and track entry state."""
    # ... existing position update logic ...

    # NEW: Track entry state
    self.state.record_entry(side, price, velocity_bps)
```

---

### 6. SAFE HEDGE TARGET UPDATES VIA CANCEL-AND-REPLACE

**Current Mechanism (live_trading.py lines 777-991):**
The engine already has `cancel_and_replace()` for safe order updates:
- Tracks pending orders by market_slug + side
- Captures partial fills during cancellation
- Prevents duplicate orders

**Gap:**
The spread capture cycle doesn't use cancel-and-replace for hedge target updates.

**Current Flow (run_paper_bot.py lines 4486-4548):**
```python
for quote in quotes:
    result = await self._engine.execute_single_side_trade(
        market=market, side=side, price=price, size=size
    )
```

**Proposed Enhancement:**
```python
for quote in quotes:
    pending_key = f"{market.slug}_{quote['side']}"

    if pending_key in self._pending_order_ids:
        # Use cancel-and-replace for safe price update
        result = await self._engine.cancel_and_replace(
            market=market,
            side=quote['side'],
            new_price=quote['price'],
            new_size=quote['size'],
            price_tolerance=0.005,  # Only update if price changed >0.5%
            stale_seconds=5.0,       # Allow faster updates for spread capture
        )
    else:
        # New order
        result = await self._engine.execute_single_side_trade(...)
```

**Benefit:**
- Prevents order thrashing
- Captures fills during updates
- Maintains position accuracy

---

### 7. CSV LOGGING MATCHING OBSERVER SCHEMA

**Current Gap:**
Live trading has limited logging compared to observer's comprehensive CSV output.

**Observer Schema (spread_capture_observer.py lines 408-424):**
```python
headers = [
    'timestamp_ms', 'market_slug', 'time_remaining_secs',
    'binance_price', 'velocity_bps',
    'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
    'zone', 'entry_signal', 'entry_side',
    'up_offset', 'down_offset',
    'entry_price', 'hedge_price',
    'would_fill_entry', 'would_fill_hedge',
    'up_pos', 'down_pos', 'pairs', 'locked_profit',
]
```

**Proposed Addition to run_paper_bot.py:**
```python
async def _log_spread_capture_state(self, market, velocity_bps, quotes, position):
    """Log detailed state for offline analysis."""
    log_entry = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'market_slug': market.slug,
        'velocity_bps': velocity_bps,
        'velocity_zone': self._get_velocity_zone(velocity_bps),
        'up_bid': orderbook.up_bid,
        'up_ask': orderbook.up_ask,
        'down_bid': orderbook.down_bid,
        'down_ask': orderbook.down_ask,
        'pair_cost': position.pair_cost if position else None,
        'up_shares': position.up_shares if position else 0,
        'down_shares': position.down_shares if position else 0,
        'locked_hedge_target': strategy.state.locked_hedge_target,
        'quotes_generated': len(quotes),
    }
    self._csv_logger.write(log_entry)
```

---

### 8. MARKET RESET STATE MANAGEMENT

**Current State (spread_capture.py lines 920-933):**
```python
def reset(self) -> None:
    """Reset state for new market, preserving statistics."""
    s = self.state
    # Reset position
    s.up_shares = 0
    s.down_shares = 0
    # ... but doesn't reset entry tracking
```

**Gap:**
If we add entry state tracking (Improvement #5), must reset on market change.

**Proposed Enhancement:**
```python
def reset(self) -> None:
    """Reset state for new market."""
    s = self.state
    # Reset position
    s.up_shares = 0
    s.down_shares = 0
    s.up_cost = 0.0
    s.down_cost = 0.0
    s.up_avg_price = 0.0
    s.down_avg_price = 0.0

    # NEW: Reset entry tracking
    s.entry_side = None
    s.entry_velocity_direction = None
    s.locked_hedge_target = None

    # Reset phase
    s.phase = SpreadCapturePhase.IDLE
```

---

## Implementation Priority

| # | Improvement | Impact | Effort | Priority |
|---|-------------|--------|--------|----------|
| 1 | Multi-zone dynamic hedge tightening | +$10.80/8hr | Medium | **P0** |
| 2 | No order pulling | Prevents -$33 loss | Low | **P0** |
| 3 | Fixed hedge target pattern | Prevents chasing | Low | **P1** |
| 4 | Configurable parameters | Enables tuning | Medium | **P1** |
| 5 | Entry state tracking | Enables #1 | Medium | **P1** |
| 6 | Cancel-and-replace for updates | Safer execution | Low | **P2** |
| 7 | CSV logging | Enables analysis | Low | **P2** |
| 8 | Market reset management | Prevents bugs | Low | **P2** |

---

## Implementation Order

### Phase 1: Core Logic (P0 items)
1. Add VELOCITY_ZONES dict to spread_capture.py
2. Implement get_dynamic_hedge_target() method
3. Remove order pulling from run_paper_bot.py
4. Add entry state tracking to SpreadCaptureState

### Phase 2: Integration (P1 items)
5. Add SpreadCaptureParams dataclass
6. Integrate dynamic hedge target into on_fill()
7. Add maybe_tighten_hedge_target() call in trading cycle

### Phase 3: Polish (P2 items)
8. Use cancel-and-replace for hedge target updates
9. Add CSV logging matching observer schema
10. Ensure proper market reset

---

## Verification Plan

After implementation, run observer for 4+ hours and verify:

| Metric | Target | How to Verify |
|--------|--------|---------------|
| Avg Pair Cost | < $0.94 | CSV analysis |
| Win Rate | > 95% | Count profitable merges |
| Hedge Fill Rate | > 95% | Count unhedged positions |
| Hourly Profit | > $3.50/hr | Balance tracking |
| No Order Churn | 0 pulled orders | Log analysis |

---

## Risk Mitigation

1. **Deploy to observer first** - Validate logic before live trading
2. **Feature flag** - Enable/disable dynamic tightening
3. **Monitor pair costs** - Alert if avg > $0.97
4. **Gradual rollout** - Test on single market first
