# Handoff Document - Polymarket AMM Bot

**Created**: 2025-12-20 21:20 PST

---

## Original Task

Implement a new "Directional Mode" for the Polymarket AMM trading bot that replicates the user's manual trading style:
1. Start with a bias (BULLISH or BEARISH) specified via CLI
2. Accumulate shares on the bias side
3. If price moves against, average down on bias side
4. If sustained impulsive move detected (via std dev of Binance BTCUSDT), flip bias and gradually hedge
5. Priority 1 is always hedging to get pair cost < $0.95
6. Emergency hedge with <5 mins remaining
7. Time-decayed attractive price threshold (accept higher prices as time runs out)

---

## Work Completed

### 1. Created Binance WebSocket Client
**File**: `/Users/rananjaybika/polymarket-amm-bot/src/api/binance_client.py` (NEW - 323 lines)

- Real-time BTCUSDT price feed via public WebSocket (no API key required)
- Strike price tracking (reference at market open)
- Rolling window statistics for flip detection
- Key methods:
  - `connect()` / `disconnect()` - WebSocket lifecycle
  - `set_strike_price()` - Set reference price at market open
  - `get_std_dev(window_seconds)` - Standard deviation of price changes
  - `calculate_z_score(window_seconds)` - Z-score for flip detection
  - Properties: `current_price`, `strike_price`, `price_vs_strike_pct`

### 2. Created Directional Strategy Service
**File**: `/Users/rananjaybika/polymarket-amm-bot/src/services/directional_strategy.py` (NEW - 636 lines)

**Components**:
- `DirectionalConfig` - All configuration parameters
- `FlipDetector` - Time-decay adjusted flip detection
- `DirectionalTradingStrategy` - Main trading logic with phases

**Trading Phases**:
1. `ACCUMULATE` - Buy bias side at attractive prices
2. `REBALANCE` - Priority hedge to balance position (pair cost < $0.95)
3. `AVERAGE_DOWN` - After balanced, improve pair cost on bias side
4. `EMERGENCY_HEDGE` - <5 mins left, immediate full hedge
5. `BALANCED` - Fully hedged, wait for resolution

**Flip Detection Algorithm**:
```python
time_factor = time_remaining / 900  # 0.0 to 1.0
adjusted_sigma = 1.5 + (1.0 * time_factor)  # 1.5σ late, 2.5σ early
time_conviction = (1 - time_factor) * abs(price_vs_strike_pct) * 10

# Flip if: (z_score > adjusted_sigma OR time_conviction > 5.0) AND sustained > 30s
```

**Time-Decayed Attractive Price** (latest feature):
```python
def _get_attractive_price(self) -> float:
    time_factor = min(1.0, max(0.0, self._time_remaining_secs / 900))
    early = self.config.attractive_price_early  # $0.75
    late = self.config.attractive_price_late    # $0.90
    attractive = early + (late - early) * (1 - time_factor)
    return attractive
```

| Time Remaining | Threshold |
|----------------|-----------|
| 15 mins | $0.75 |
| 10 mins | $0.80 |
| 5 mins | $0.85 |
| 2 mins | $0.88 |
| 0 mins | $0.90 |

### 3. Modified run_paper_bot.py
**File**: `/Users/rananjaybika/polymarket-amm-bot/scripts/run_paper_bot.py`

**Changes**:
- Added imports for directional mode components (lines 51-57)
- Added directional mode parameters to `__init__` (lines 92-95, 118-121)
- Added `_directional_trading_cycle()` method for directional trading logic
- Added CLI arguments (lines 1868-1923):
  - `--directional` - Enable directional mode
  - `--bias BULLISH|BEARISH` - Initial trading bias
  - `--sigma-threshold` - Std devs for flip (default: 2.0)
  - `--sustained-seconds` - Seconds to confirm flip (default: 30)
  - `--window-seconds` - Rolling window for stats (default: 60)
  - `--attractive-price-early` - Max price early in market (default: 0.75)
  - `--attractive-price-late` - Max price late in market (default: 0.90)
  - `--hedge-increment` - Shares per hedge cycle (default: 5)
  - `--flip-cooldown` - Seconds between flips (default: 60)
  - `--max-position-pct` - Max shares per side as % of balance (default: 15%)
  - `--pair-cost-target` - Target pair cost for hedging (default: 0.95)

### 4. Test Run Successful
Bot was tested and executed trades:
```
Single-side UP trade: 5/5 filled @ $0.7601, cost=$3.8005, balance=$96.20
Single-side UP trade: 2/5 filled @ $0.7703, cost=$1.5407, balance=$94.66
Single-side DOWN trade: 5/5 filled @ $0.2400, cost=$1.2001, balance=$93.46
```

---

## Work Remaining

### Immediate
None - the directional mode implementation is complete and tested.

### Future Enhancements (Not Requested Yet)
1. **CSV Logging Extension**: Add directional-specific columns:
   - `btc_price`, `strike_price`, `price_vs_strike_pct`, `bias`, `phase`, `flip_count`, `z_score`, `sustained_secs`

2. **Emergency Balancing Before Expiry**: Force buy at any price in final 30-60 seconds (user said "not yet" when asked)

3. **Live Display Mode**: Real-time terminal display of directional metrics

---

## Attempted Approaches

### Issue: Bot Not Trading Initially
- **Problem**: First test run showed no trades because prices ($0.51) were above the attractive threshold ($0.55)
- **Solution**: User requested time-decayed attractive price - accept <$0.75 early, <$0.90 late
- **Implementation**: Added `_get_attractive_price()` method with linear interpolation

### Issue: AttributeError on Startup
- **Problem**: After renaming `attractive_price` to `attractive_price_early`/`attractive_price_late`, old references remained
- **Locations Fixed**:
  - `directional_strategy.py:503` - `_evaluate_accumulate()`
  - `directional_strategy.py:576` - `_evaluate_average_down()`
  - `run_paper_bot.py:931` - Startup logging
  - `run_paper_bot.py:1956` - DirectionalConfig instantiation

---

## Critical Context

### Key Design Decisions

1. **Hedging Priority**: Priority 1 is ALWAYS hedging to get pair cost < $0.95, not accumulating on bias side

2. **Emergency Hedge Threshold**: <5 minutes remaining (user changed from original 3 minutes)

3. **Max Share Price Hard Cap**: $0.95 - Never buy above this, even in emergency hedge. Better to accept unhedged loss than lock in guaranteed loss.

4. **Max Position Per Side**: 15% of starting balance (e.g., $100 balance = max 15 shares per side)

5. **Flip Detection**: Uses Binance BTCUSDT spot price, NOT Polymarket prices. Strike = close price at market open (e.g., 1:00pm EST for 15-min market)

### Configuration Defaults
```python
@dataclass
class DirectionalConfig:
    sigma_threshold: float = 2.0          # Base std devs for flip
    sustained_seconds: float = 30.0       # Seconds to confirm flip
    window_seconds: int = 60              # Rolling window for stats
    flip_cooldown_seconds: float = 60.0   # Min time between flips
    max_position_pct: float = 0.15        # 15% of balance per side
    attractive_price_early: float = 0.75  # Max price early in market
    attractive_price_late: float = 0.90   # Max price late in market
    dip_threshold_pct: float = 0.02       # 2% dip to average down
    trade_size: int = 5                   # Shares per trade
    hedge_increment: int = 5              # Shares per hedge cycle
    max_share_price: float = 0.95         # Never buy above
    pair_cost_target: float = 0.95        # Target pair cost
    emergency_threshold_secs: int = 300   # 5 minutes
```

### Backup Location
Full-featured version (with all 4 trading modes) backed up at:
`/Users/rananjaybika/polymarket-amm-bot-full-version/`

Current version only has Accumulation Mode + new Directional Mode.

---

## Current State

### Status: COMPLETE

All requested features implemented and tested:
- [x] Binance WebSocket client for BTCUSDT price feed
- [x] Strike price tracking at market open
- [x] Flip detection with time decay (easier to flip late in market)
- [x] Trading phases (accumulate → rebalance → average_down → emergency_hedge → balanced)
- [x] Emergency hedge with <5 mins remaining
- [x] Max 15% of balance per side
- [x] Never buy above $0.95 (even emergency hedge)
- [x] Time-decayed attractive price ($0.75 early → $0.90 late)
- [x] CLI arguments for all parameters

### Run Command
```bash
# 2-hour run (120 minutes)
python scripts/run_paper_bot.py --directional --bias BULLISH --duration 120

# Background run
nohup python scripts/run_paper_bot.py --directional --bias BULLISH --duration 120 > bot_output.log 2>&1 &

# Check progress
tail -f bot_output.log
```

### Files Modified This Session
| File | Status |
|------|--------|
| `src/api/binance_client.py` | NEW (323 lines) |
| `src/services/directional_strategy.py` | NEW (636 lines) |
| `scripts/run_paper_bot.py` | MODIFIED (added ~200 lines) |

### No Running Instances
User was provided command to run at 9:30pm PST for 2 hours.
