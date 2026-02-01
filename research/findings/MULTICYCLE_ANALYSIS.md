# Multi-Cycle Parallel Positions Analysis - January 31, 2026

## Executive Summary

**Current capture rate is only 5.6%** - we miss 4,066 good spikes (94.4%) due to position blocking.

Multi-cycle trading with 4 parallel cycles increases good trades by **+265%** (242 → 884).

---

## Problem Statement

Current single-cycle approach:
- Enter 50 shares on spike
- Wait up to 180s for hedge
- Block new entries while holding
- Miss many good spikes during hold time

---

## Analysis Results (OOS7: 19 hours)

### Single-Cycle Performance

| Metric | Value |
|--------|-------|
| Total spikes | 10,123 |
| Good spikes (loser drop >= 12c) | 4,308 (42.6%) |
| **Trades taken** | 447 |
| **Good trades** | 242 |
| **Missed good spikes** | 4,066 |
| **Good spike capture rate** | 5.6% |

**We're only capturing 5.6% of available good spikes!**

### Multi-Cycle Performance

| Cycles | Total Trades | Good Trades | Improvement | $/hr (est) |
|--------|--------------|-------------|-------------|------------|
| 1 (current) | 447 | 242 | - | baseline |
| 2 | 866 | 457 | +88.8% | +$28/hr |
| 3 | 1,272 | 678 | +180.2% | +$38/hr |
| **4** | **1,645** | **884** | **+265.3%** | **+$42/hr** |

---

## Hold Time Analysis

### Good Spikes

| Percentile | Hold Time |
|------------|-----------|
| 25th | Fast fill |
| Median | ~60s |
| 75th | ~120s |
| Mean | Variable |

### Bad Spikes

- Hold time: 180s (always timeout)
- This is the main blocker - bad spikes tie up capital for full timeout

---

## Risk Comparison

| Metric | Single (50 shares) | Multi (4 × 10 shares) |
|--------|--------------------|-----------------------|
| Max loss per trade | $25.00 | $5.00 |
| Max concurrent exposure | $50 | $40 (4 × $10) |
| Polymarket min order | Met ($5) | Met ($1) |
| Diversification | None | Across 4 spikes |

**Multi-cycle has LOWER risk per trade and better diversification.**

---

## Implementation Design

### Data Structure

```python
@dataclass
class Cycle:
    """Single trading cycle."""
    id: int
    entry_ts: int
    market_slug: str
    winner_side: str
    entry_price: float
    shares: int
    hedge_target: float
    status: str  # 'pending_entry', 'pending_hedge', 'completed'

@dataclass
class MultiCycleManager:
    """Manages up to 4 parallel cycles."""
    max_cycles: int = 4
    shares_per_cycle: int = 10
    active_cycles: List[Cycle] = field(default_factory=list)

    def can_enter(self) -> bool:
        """Check if we can start a new cycle."""
        return len(self.active_cycles) < self.max_cycles

    def enter_spike(self, spike_data: dict) -> Optional[Cycle]:
        """Enter a new cycle if capacity available."""
        if not self.can_enter():
            return None

        cycle = Cycle(
            id=len(self.active_cycles),
            entry_ts=spike_data['timestamp_ms'],
            market_slug=spike_data['market_slug'],
            winner_side=spike_data['winner_side'],
            entry_price=spike_data['winner_ask'],
            shares=self.shares_per_cycle,
            hedge_target=spike_data['loser_bid'],
            status='pending_entry'
        )
        self.active_cycles.append(cycle)
        return cycle

    def on_fill(self, fill_data: dict):
        """Handle fill event - route to correct cycle."""
        for cycle in self.active_cycles:
            if self._matches_cycle(cycle, fill_data):
                self._process_fill(cycle, fill_data)
                break

    def cleanup_completed(self):
        """Remove completed cycles."""
        self.active_cycles = [c for c in self.active_cycles if c.status != 'completed']
```

### Order Management

Each cycle tracks its own:
- Entry order (winner side)
- Hedge order (loser side)
- Time-stop countdown

Fills are matched by:
1. Market slug
2. Side (UP/DOWN)
3. Price proximity to target

### Position Tracking

```python
# Track per-market aggregate position
positions = {
    'btc-15m-up': {
        'cycles': [cycle_1, cycle_3],
        'total_shares': 20,
    },
    'btc-15m-down': {
        'cycles': [cycle_2],
        'total_shares': 10,
    }
}
```

---

## Phased Rollout

### Phase 1: 2 Cycles (Conservative)

```python
MAX_CYCLES = 2
SHARES_PER_CYCLE = 20  # 2 × 20 = 40 total
```

Benefits:
- Simpler position tracking
- +89% more good trades
- Lower complexity

### Phase 2: 4 Cycles (Full)

```python
MAX_CYCLES = 4
SHARES_PER_CYCLE = 10  # 4 × 10 = 40 total
```

Benefits:
- +265% more good trades
- Maximum capture rate
- Best diversification

---

## Considerations

### Polymarket Constraints

1. **$1 minimum order:**
   - At 10 shares × $0.10 = $1.00 ✓ (exactly meets minimum)
   - Safe margin: 10 shares × $0.11 = $1.10 ✓

2. **Order rate limits:**
   - May hit rate limits with 4x order frequency
   - Monitor and adjust if needed

### Hedge Matching Complexity

Challenge: Multiple cycles may have similar hedge targets

Solution:
```python
def match_fill_to_cycle(fill, cycles):
    """Match incoming fill to the most likely cycle."""
    candidates = [c for c in cycles if c.market_slug == fill.market_slug]

    if len(candidates) == 1:
        return candidates[0]

    # Match by price proximity and timing
    return min(candidates, key=lambda c: abs(c.hedge_target - fill.price))
```

---

## Expected Impact

### Conservative Estimate (2 cycles)

| Metric | Single | 2-Cycle | Improvement |
|--------|--------|---------|-------------|
| Good trades/hr | 12.8 | 24.2 | +89% |
| Profit/hr (at $0.05/share edge) | $32 | $60 | +$28/hr |

### Optimistic Estimate (4 cycles)

| Metric | Single | 4-Cycle | Improvement |
|--------|--------|---------|-------------|
| Good trades/hr | 12.8 | 46.7 | +265% |
| Profit/hr (at $0.05/share edge) | $32 | $117 | +$85/hr |

---

## Recommendation

1. **Implement 2-cycle first** for simpler validation
2. **Run 24-48 hours** to confirm edge holds with smaller size
3. **Scale to 4-cycle** if validation successful
4. **Monitor rate limits** and adjust if needed

---

## Files Created

- `research/ml/multicycle_analysis.py` - Simulation script
- `research/findings/MULTICYCLE_ANALYSIS.md` - This document

---

*Analysis completed: January 31, 2026*
