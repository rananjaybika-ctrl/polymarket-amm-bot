# Summary 03-02: Position Tracking

## Status: Complete

## What Was Built

### 1. Position Model (`src/models/position.py`)

**Fill dataclass:**
- `token_id`, `side`, `price`, `size`, `timestamp`
- `cost` property for fill cost calculation

**Position dataclass:**
- Tracks both Up and Down tokens for a market
- Average price calculation via weighted averaging
- Fill history tracking

**Key Properties:**
- `pair_count`: Matched pairs (min of Up/Down)
- `unmatched_up/down`: Directional exposure
- `is_balanced`: True if Up == Down
- `pair_cost`: Average cost per pair
- `unrealized_pnl`: Profit at resolution ($1.00 - pair_cost)
- `unrealized_pnl_percent`: P&L as percentage

**Methods:**
- `add_fill(side, price, size)`: Record fill with averaging
- `sync_balances(up, down)`: Sync from chain state
- `to_dict()`: Serialize for logging

### 2. PositionTracker Service (`src/services/position_tracker.py`)

**Core Methods:**
- `sync_position(market)`: Fetch balances from chain
- `add_fill(market, side, price, size)`: Record single fill
- `add_pair_fill(market, up_price, down_price, size)`: Record pair
- `get_position(market)`: Get position for market
- `get_all_positions()`: List all positions

**Portfolio Management:**
- `get_portfolio_summary()`: Aggregate portfolio stats
- `get_active_positions()`: Positions with balances
- `get_imbalanced_positions()`: Positions needing rebalance
- `get_total_pnl()`: Sum of all P&L
- `get_total_exposure()`: Directional risk

**PortfolioSummary dataclass:**
- Total positions, pairs, cost
- Unrealized P&L with percentage
- USDC balance
- Directional exposure (Up/Down)

### 3. Test Script (`scripts/test_position_tracking.py`)

Validates:
- USDC balance fetching
- Position sync from chain
- Simulated fills with averaging
- P&L calculation
- Portfolio summary
- Imbalance detection

## Test Results

```
1. Fetching USDC Balance
   USDC Balance: $99.78 ✓

2. Syncing Position Balances from Chain
   3 markets synced (all 0 balance) ✓

3. Testing Simulated Fills
   10 pairs @ $0.51 each = $1.02/pair
   5 pairs @ $0.49 each = $0.98/pair
   Average: $1.0067/pair ✓
   Unrealized PnL: -$0.10 (correct for >$1 cost) ✓

4. Portfolio Summary
   Total Pairs: 15.0
   Total Cost: $15.10
   USDC Balance: $99.78
   Total Value: $114.78 ✓

5. Imbalance Detection
   Created 5 Up imbalance
   Needs Rebalance: True ✓
```

## Integration Flow

```
┌─────────────┐     ┌────────────────┐     ┌──────────────┐
│OrderExecutor│────▶│PositionTracker │────▶│   Position   │
│ (on fill)   │     │ (track state)  │     │ (calculate)  │
└─────────────┘     └────────────────┘     └──────────────┘
                            │
                    sync from chain
                            ▼
                    ┌──────────────┐
                    │PolymarketAPI │
                    │(get_position)│
                    └──────────────┘
```

**Production Workflow:**
1. OrderExecutor fills pair order
2. `tracker.add_pair_fill(market, prices, size)`
3. Position updates with weighted averages
4. P&L calculated automatically
5. Periodic `sync_position()` verifies chain state

## Files Created
- `src/models/position.py`
- `src/services/position_tracker.py`
- `scripts/test_position_tracking.py`
- `.planning/phases/03-trading-core/03-02-PLAN.md`

## Files Modified
- `src/models/__init__.py` (exports Position, Fill)
- `src/services/__init__.py` (exports PositionTracker, PortfolioSummary)

## Verification Checklist
- [x] Can fetch position balances from chain
- [x] Position tracking updates on fills
- [x] Weighted average price calculation works
- [x] P&L calculation is accurate
- [x] Imbalance detection works
- [x] Portfolio summary aggregates correctly
- [x] Test script demonstrates all functionality

## Key Insights

**P&L Formula:**
- For balanced pairs: `unrealized_pnl = pair_count * ($1.00 - pair_cost)`
- Positive when pair_cost < $1.00 (our target)
- Negative when pair_cost > $1.00 (as shown in tests)

**Weighted Averaging:**
- New avg = (old_total_cost + new_cost) / new_balance
- Works correctly across multiple fills

## Next Steps
Plan 03-03: Balance Management
- Recovery logic for imbalanced positions
- Auto-rebalancing strategies
- Risk limits and position sizing
