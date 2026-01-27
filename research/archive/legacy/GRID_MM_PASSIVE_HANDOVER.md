# Grid MM Passive Strategy - Handover Document

**Date:** January 16, 2026
**Context Window:** Low - comprehensive handover for new session

---

## Executive Summary

We developed a **passive grid market making strategy** for Polymarket BTC Up/Down 15-minute markets. Backtesting shows **$95/hr profit** with 100% profitable pairs.

**Key breakthrough:** All bids must be PASSIVE (below best_bid). No aggressive winner posting. No order pulling.

---

## Strategy Overview

### Core Concept
1. Post BID orders on BOTH UP and DOWN sides (two-sided MM)
2. Wait for market to drop to our prices (MAKER fills)
3. When both sides fill, pair_cost < $1.00 = profit at settlement
4. Velocity determines LOSER bid depth (deeper = cheaper fills)

### Formula
```
our_bid = best_bid - offset
```
- **Positive offset** = bid BELOW best_bid (passive, cheaper)
- ALL offsets are positive (0.01 to 0.05)

### Velocity Zones
```
|v| < 0.10:   winner=0.01, loser=0.01  (symmetric)
|v| 0.10-0.30: winner=0.01, loser=0.01  (same)
|v| 0.30-0.50: winner=0.01, loser=0.03  (loser deeper)
|v| >= 0.50:   winner=0.01, loser=0.05  (loser very deep)
```

---

## Backtest Results

**Data:** 199,434 observations, 46 markets, 11.5 hours

| Strategy | Pairs | Avg Cost | Hourly Rate | Win % |
|----------|-------|----------|-------------|-------|
| Static (0.01 both) | 373 | $0.8087 | $93.23/hr | 100% |
| Velocity-adjusted | 323 | $0.7738 | **$95.47/hr** | 100% |

**Why velocity wins:** Fewer pairs but BETTER prices on loser side.

---

## Critical Learnings

### What Works
1. **Passive bids only** - posting below best_bid captures spread
2. **NO order pulling** - let orders sit until filled
3. **Velocity adjusts loser depth** - deeper loser bids = cheaper fills
4. **Proper order tracking** - track posted price, don't recalculate each tick

### What Doesn't Work
1. **Aggressive winner bids** - posting above best_bid causes expensive fills
2. **Order pulling on zone changes** - disrupts fill flow, misses opportunities
3. **Recalculating bids each tick** - changes posted price unrealistically

---

## Files Created/Modified

### Strategy Implementation (NEW)
| File | Purpose |
|------|---------|
| `/src/strategies/grid_mm_passive.py` | **NEW** - Main strategy class |
| `/scripts/run_grid_mm_passive.py` | **NEW** - Runner script (paper + live modes) |

### Research Scripts
| File | Purpose |
|------|---------|
| `research/grid_mm_velocity_backtest.py` | Main backtest with velocity zones |
| `research/trading_examples_analysis.py` | Market type analysis |
| `research/VELOCITY_GRID_MM_EXPLAINED.md` | Strategy explanation |
| `research/GRID_MM_PNL_PLAN.md` | Original plan (outdated) |
| `research/ORDER_FLOW_FINDINGS.md` | Order flow analysis |

### Key Configuration in Backtest
```python
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.10, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'moderate':     {'vel_min': 0.10, 'vel_max': 0.30, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'strong':       {'vel_min': 0.30, 'vel_max': 0.50, 'winner_offset': 0.01, 'loser_offset': 0.03},
    'very_strong':  {'vel_min': 0.50, 'vel_max': 99.0, 'winner_offset': 0.01, 'loser_offset': 0.05},
}
STATIC_OFFSET = 0.01  # For baseline comparison
```

---

## Observer Status

**NO CHANGES NEEDED** - existing observer captures all required fields:
- Location: `/scripts/spread_capture_observer.py`
- Columns: up_bid, up_ask, down_bid, down_ask, velocity_bps, time_remaining_secs, market_slug
- Data: `/research/observer/*.csv`

---

## Next Steps

### ✅ 1. Strategy Created
- `/src/strategies/grid_mm_passive.py` - Simple two-sided MM
- `/scripts/run_grid_mm_passive.py` - Runner script

### 2. Test Manually ($30 risk)
- Post 2 orders via Polymarket UI (UP + DOWN at best_bid - 0.01)
- Wait for fills
- Verify pair_cost < $1.00
- Hold until settlement

### 3. Run Paper Mode
```bash
python scripts/run_grid_mm_passive.py --paper --hours 2
```

### 4. Run Live Mode (after manual test)
```bash
python scripts/run_grid_mm_passive.py --live --hours 2
```
NOTE: Live order placement not yet implemented - needs Polymarket API integration

---

## Architecture Reference

From `spread_capture.py` (use for market connection):
- `BTCMarket` dataclass for market data
- `BinanceClient` for velocity
- `PolymarketWebSocket` for orderbook
- `MarketFinder` for market discovery

---

## Warnings

1. **DO NOT use aggressive offsets** (negative values that post above best_bid)
2. **DO NOT pull orders** when velocity changes
3. **DO NOT recalculate** posted bid prices each tick
4. **Minimum tick size** is $0.01 - no $0.005 offsets allowed

---

## Session Notes

- Debugged fill detection logic extensively
- Order pulling was causing $95/hr loss vs $95/hr profit
- Static passive approach works best, velocity adds marginal improvement
- Observer data quality is good, no changes needed

---

*Handover complete - ready for new implementation session*
