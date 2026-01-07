# Summary 04-01: Simulation Engine

## Status: Complete

## What Was Built

### PaperTradingEngine (`src/services/paper_trading.py`)

**Core Features:**
- `execute_paper_trade(opportunity, size)`: Simulate pair trade execution
- `get_position(market)`: Get paper position for a market
- `get_total_pnl()`: Calculate unrealized P&L
- `resolve_market(slug, winner)`: Simulate market resolution
- `reset()`: Reset simulation state

**Simulation Parameters (SimulationConfig):**
- `fill_probability`: Base fill rate (default 90%)
- `partial_fill_rate`: Chance of partial fill (default 10%)
- `slippage_bps`: Max slippage in basis points (default 5)
- `random_seed`: Optional seed for reproducible tests

### Data Models

**PaperPosition:**
- Tracks Up/Down token sizes and costs
- Calculates pair count, average prices
- Computes expected profit

**SimulationStats:**
- Total trades, successful pairs, partial fills, failed fills
- Win rate, total cost, total profit
- Realized P&L after market resolution

## Simulation Logic

```
1. Receive opportunity + size
2. Simulate Up order:
   - Roll fill probability (90%)
   - Determine fill type (full/partial/none)
   - Apply slippage to price
3. Simulate Down order (same process)
4. Update paper position
5. Calculate expected P&L
```

## Fill Simulation

| Roll | Result |
|------|--------|
| 0.00 - 0.10 | Partial fill (30-90% filled) |
| 0.10 - 0.90 | Full fill |
| 0.90 - 1.00 | No fill |

## Usage

```python
from src.services.paper_trading import PaperTradingEngine, SimulationConfig

# Create engine
config = SimulationConfig(fill_probability=0.90)
engine = PaperTradingEngine(config=config, initial_balance=100.0)

# Execute paper trade
result = await engine.execute_paper_trade(opportunity, size=10)

if result.success:
    print(f"Paper trade executed: ${result.actual_cost:.4f}")

# Check P&L
print(f"Unrealized P&L: ${engine.get_total_pnl():.4f}")

# Resolve market
pnl = engine.resolve_market(market.slug, "UP")
print(f"Realized P&L: ${pnl:.4f}")
```

## Test Script
```bash
python scripts/test_paper_trading.py
```

## Files Created
- `src/services/paper_trading.py`
- `scripts/test_paper_trading.py`
- `.planning/phases/04-dry-run/04-01-PLAN.md`

## Files Modified
- `src/services/__init__.py` (exports PaperTradingEngine, etc.)

## Verification Checklist
- [x] Paper trades execute without API calls
- [x] Fill probability simulation works
- [x] Partial fills simulated correctly
- [x] Slippage applied to prices
- [x] Positions tracked accurately
- [x] P&L calculation correct
- [x] Market resolution works
- [x] Statistics tracked

## Next: 04-02 Strategy Validation
Run the full trading loop through multiple market cycles to validate profitability.
