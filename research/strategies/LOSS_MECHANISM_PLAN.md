# Loss Mechanism Design: Per-Trade + Session Circuit Breaker

**Date:** February 1, 2026
**Status:** Planning

---

## Context

### Mathematical Profitability Analysis
The AGGRESSIVE strategy **IS mathematically profitable (+EV)**:

| Metric | Value |
|--------|-------|
| **Expected Value Per Trade** | **+$0.57** |
| Win Rate (PnL basis) | 44.85% |
| Average Win | +$4.19 |
| Average Loss | -$2.36 |
| Win/Loss Ratio | 1.77x |
| Breakeven Win Rate | 36.1% |
| Actual Win Rate | 44.9% |
| **Safety Margin** | **+8.7pp** |

**Key Insight:** The 71% mentioned earlier is the **passive fill rate**, not win rate. Actual PnL win rate is 44.85%, but still above the 36.1% breakeven threshold.

### The Asymmetry Problem
- Wins capped at ~$4-5 (passive fills)
- Losses can reach -$37 (time-stop exits in adverse conditions)
- **Worst losses are ALL time-stop exits** (not hedging failures)

### Current Loss Mechanisms (What Exists)

| Layer | Mechanism | Status |
|-------|-----------|--------|
| Per-trade | 12% stop-loss (`check_stop_loss()`) | Disabled for AGGRESSIVE |
| Per-trade | 180s time-stop | Active |
| Session | Daily loss limit ($50) | Active in `balance_manager.py` |
| Session | Consecutive losses (3) | Configured but not wired |
| Position | Max imbalance (10 shares) | Active |

### Gaps to Fill
1. **No per-trade unrealized loss limit** during active position
2. **No drawdown-based circuit breaker** (peak-to-trough)
3. **No loss acceleration detection**

---

## Proposed Design

### 1. Per-Trade Unrealized Loss Limit ($10)

**Problem:** Current time-stop waits 180s regardless of how badly the trade is going. A trade can lose $30+ before time-stop triggers.

**Solution:** Add **early exit** if unrealized loss exceeds threshold BEFORE time-stop.

```python
# In enhanced_spike.py - new method
def check_unrealized_loss_limit(
    self,
    winner_current_bid: float,
    shares: int = 50,
    max_unrealized_loss: float = 10.0,  # $10 default
) -> Tuple[bool, float]:
    """
    Check if unrealized loss exceeds limit.

    Returns:
        (should_exit, current_unrealized_loss)
    """
    if self.first_fill_price is None:
        return False, 0.0

    # Unrealized loss = (entry - current) * shares
    unrealized_loss = (self.first_fill_price - winner_current_bid) * shares

    if unrealized_loss >= max_unrealized_loss:
        return True, unrealized_loss

    return False, unrealized_loss
```

**Integration point:** Call in `get_quotes()` BEFORE time-stop check (line ~1660).

**Parameters:**
- `max_unrealized_loss`: $10 default (configurable)
- Based on 50 shares: triggers at ~$0.20 adverse move

### 2. Session Circuit Breaker (Drawdown-Based)

**Problem:** Current daily loss limit only tracks realized losses. Doesn't detect "bad streaks" or accelerating losses.

**Solution:** Multi-level circuit breaker in `balance_manager.py`:

```python
class SessionRiskManager:
    """Track session-level risk metrics."""

    def __init__(
        self,
        max_session_loss: float = 100.0,      # Hard stop
        drawdown_pause_pct: float = 0.15,     # 15% drawdown -> pause
        cooloff_minutes: int = 30,            # Pause duration
        loss_acceleration_window: int = 5,    # Recent trades to check
    ):
        self.peak_balance = 0.0
        self.session_realized_pnl = 0.0
        self.recent_trades: List[float] = []  # Last N trade PnLs
        self.paused_until: Optional[datetime] = None

    def record_trade(self, pnl: float, current_balance: float) -> RiskAction:
        """Record trade and return risk action."""
        self.session_realized_pnl += pnl
        self.recent_trades.append(pnl)
        if len(self.recent_trades) > 20:
            self.recent_trades.pop(0)

        # Update peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance

        # Check conditions
        if self._check_hard_stop():
            return RiskAction.STOP_SESSION
        if self._check_drawdown_pause(current_balance):
            return RiskAction.PAUSE_30_MIN
        if self._check_loss_acceleration():
            return RiskAction.REDUCE_SIZE_50PCT

        return RiskAction.CONTINUE

    def _check_hard_stop(self) -> bool:
        return self.session_realized_pnl <= -self.max_session_loss

    def _check_drawdown_pause(self, current_balance: float) -> bool:
        if self.peak_balance <= 0:
            return False
        drawdown_pct = (self.peak_balance - current_balance) / self.peak_balance
        return drawdown_pct >= self.drawdown_pause_pct

    def _check_loss_acceleration(self) -> bool:
        """Detect if losses are accelerating."""
        if len(self.recent_trades) < 10:
            return False
        recent_5 = sum(self.recent_trades[-5:])
        older_5 = sum(self.recent_trades[-10:-5])
        # Accelerating if recent losses are 2x worse
        return recent_5 < older_5 * 2 and recent_5 < -10
```

**Risk Actions:**
| Condition | Action |
|-----------|--------|
| Session loss >= $100 | **STOP_SESSION** - No more trading today |
| Drawdown >= 15% | **PAUSE_30_MIN** - Cool off period |
| Loss acceleration | **REDUCE_SIZE_50PCT** - Halve position size |

### 3. Configuration Updates

**Add to TRADING_CONFIGS.py:**
```python
# Risk Management (NEW)
max_unrealized_loss_per_trade: float = 10.0   # Per-trade loss limit
max_session_loss: float = 100.0               # Session hard stop
drawdown_pause_pct: float = 0.15              # 15% drawdown -> pause
cooloff_minutes: int = 30                     # Pause duration
```

**Add to enhanced_spike.py constructor:**
```python
max_unrealized_loss: float = 10.0,  # From config
```

---

## Files to Modify

| File | Changes | Priority |
|------|---------|----------|
| `src/strategies/enhanced_spike.py` | Add `check_unrealized_loss_limit()` method | HIGH |
| `src/strategies/enhanced_spike.py` | Integrate in `get_quotes()` before time-stop | HIGH |
| `src/services/balance_manager.py` | Add `SessionRiskManager` class | HIGH |
| `research/reference/TRADING_CONFIGS.py` | Add risk management params | MEDIUM |
| `scripts/run_paper_bot.py` | Wire `SessionRiskManager` to trading loop | MEDIUM |

---

## User Decisions

| Parameter | Decision |
|-----------|----------|
| Per-trade loss limit | **Grid search first** (test $10, $15, $20, None) |
| Session loss limit | **$50 absolute** |
| Position size reduction | **No** - keep fixed 50 shares |

---

## Implementation Plan

### Phase 1: Add Stop-Loss % to Grid Search

**File:** `research/optimizers/aggressive_grid_search.py`

**Current grid:** 2 offsets × 3 time-stops × 1 cycle-mode = 6 configs

**New grid:** 2 offsets × 3 time-stops × 4 stop-losses × 3 market-loss-limits = 72 configs

Grid dimensions:
- Offsets: TIGHT, CURRENT (2)
- Time-stops: 0s, 30s, 180s (3)
- Stop-loss %: None, 15%, 20%, 30% (4)
- Market loss limit: None, 2, 3 (3) - stop trading after N total losses in market

#### Step 1: Add stop_loss_pct and max_market_losses to TestConfig (line ~128)

```python
@dataclass
class TestConfig:
    name: str
    time_stop_seconds: float
    drop_multiplier: float
    drop_intercept: float
    offset_name: str
    stop_loss_pct: Optional[float] = None  # NEW: None = disabled, 0.20 = 20%
    max_market_losses: Optional[int] = None  # NEW: None = disabled, 2 = stop after 2 losses
    max_cycles: int = 1
    shares_per_cycle: int = 50
    cycle_mode: str = "SINGLE"
    direction_mode: str = DIRECTION_MODE_SINGLE
```

#### Step 2: Add parameter arrays (line ~166)

```python
# Time-stops to test
# 0 = no time-stop (rely on stop-loss or passive fill only)
TIME_STOPS = [0.0, 30.0, 180.0]

# Stop-loss percentages to test (NEW)
# None = disabled, percentage = exit if drop >= X%
STOP_LOSS_PCTS = [None, 0.15, 0.20, 0.30]

# Market loss limits to test (NEW)
# None = disabled, N = stop trading in market after N total losses
MAX_MARKET_LOSSES = [None, 2, 3]
```

**Grid filter:** Skip invalid configs where TIME_STOP=0 AND STOP_LOSS=None AND MAX_MARKET_LOSSES=None.

**Note:** % stop-loss makes $ unrealized loss redundant.

**Analysis: Should we stop trading in a market after big loss?**
| Threshold | Savings | Improvement |
|-----------|---------|-------------|
| < -$5 | $9.54 | 1.9% |
| < -$10 | $0.00 | 0.0% |

**Finding on BIG losses:** Bad trades are NOT clustered - worst 20 trades span 19 unique markets.

**BUT: Multiple SMALL losses ARE clustered!**

| Strategy | Savings | Improvement |
|----------|---------|-------------|
| Stop after 1 big loss (-$10) | $0.00 | 0.0% |
| **Stop after 2 losses (any size)** | **$91.66** | **18.3%** |

Top bleeding markets:
| Market | Trades | Losses | Total Loss |
|--------|--------|--------|------------|
| btc-updown-15m-1769704200 | 19 | 10 | -$41.72 |
| btc-updown-15m-1769879700 | 22 | 15 | -$33.67 |
| btc-updown-15m-1769739300 | 16 | 10 | -$32.89 |

**Consecutive vs Total losses:**
| Strategy | Savings | Improvement |
|----------|---------|-------------|
| 2 TOTAL losses | $91.66 | **18.3%** |
| 2 CONSECUTIVE losses | $77.36 | 15.4% |

**Conclusion:** Add to grid search:
1. % stop-loss (15%, 20%, 30%) - exit position early
2. Market loss counter (2 total) - stop trading in bleeding markets

#### Step 3: Update config generation (line ~182)

```python
# Generate all configs: 2 offsets × 3 time-stops × 4 stop-losses × 3 market-limits = 72 configs
CONFIGS = []
for offset_name, (mult, intercept, offset_desc) in OFFSET_PRESETS.items():
    for ts in TIME_STOPS:
        for sl_pct in STOP_LOSS_PCTS:
            for mml in MAX_MARKET_LOSSES:
                # Skip invalid: no time-stop AND no stop-loss AND no market limit
                if ts == 0 and sl_pct is None and mml is None:
                    continue

                sl_label = f"SL{int(sl_pct*100)}" if sl_pct else "NOSL"
                mml_label = f"MML{mml}" if mml else "NOMML"
                name = f"{offset_name}_TS{int(ts)}_{sl_label}_{mml_label}"

                CONFIGS.append(TestConfig(
                    name=name,
                    time_stop_seconds=ts,
                    drop_multiplier=mult,
                    drop_intercept=intercept,
                    offset_name=offset_name,
                    stop_loss_pct=sl_pct,
                    max_market_losses=mml,
                    max_cycles=1,
                    shares_per_cycle=50,
                    cycle_mode="SINGLE",
                    direction_mode=DIRECTION_MODE_SINGLE,
                ))
```

#### Step 4: Add stop-loss check in simulation (line ~564, BEFORE time-stop)

```python
# Check stop-loss FIRST (before time-stop)
if config.stop_loss_pct is not None:
    winner_side_current = position_data['winner_side']
    if winner_side_current == "UP":
        winner_bid_current = obs_row['up_bid']
    else:
        winner_bid_current = obs_row['down_bid']

    if pd.notna(winner_bid_current):
        drop_pct = (winner_entry - winner_bid_current) / winner_entry
        if drop_pct >= config.stop_loss_pct:
            # Stop-loss triggered - exit immediately
            loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
            pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                winner_entry, loser_fill, config.shares_per_cycle,
                is_taker_entry=True, is_taker_exit=True
            )
            trades.append(TradeResult(
                # ... same as time_stop but hedge_type="stop_loss"
                hedge_type="stop_loss",
                # ...
            ))
            in_position = False
            position_data = None
            last_hedge_ts = obs_ts
            obs_idx += 1
            break

# Check time-stop (existing code, line 565)
elapsed_ms = obs_ts - entry_ts
if elapsed_ms >= time_stop_ms:
    # ... existing time-stop logic
```

#### Step 5: Add market loss counter in simulation (at market level)

```python
# Track losses per market
market_loss_count = {}  # market_slug -> loss count

# Before entering a trade in a market:
if config.max_market_losses is not None:
    if market_loss_count.get(slug, 0) >= config.max_market_losses:
        # Skip this market - too many losses
        continue

# After a trade completes:
if pnl_net < 0:
    market_loss_count[slug] = market_loss_count.get(slug, 0) + 1
```

#### Step 6: Update TradeResult to track config

Add to TradeResult dataclass:
```python
stop_loss_pct: Optional[float] = None
max_market_losses: Optional[int] = None
skipped_by_mml: bool = False  # True if trade was skipped due to market loss limit
```

### Phase 2: Implement Session Circuit Breaker

**File:** `src/services/balance_manager.py`

Add to existing `BalanceManager` class:

```python
# Session tracking (NEW)
self.session_realized_pnl = 0.0
self.max_session_loss = 50.0  # User decision: $50

def record_trade_pnl(self, pnl: float) -> bool:
    """Record trade PnL and check session limit.

    Returns:
        True if within limits, False if session should stop
    """
    self.session_realized_pnl += pnl

    if self.session_realized_pnl <= -self.max_session_loss:
        logger.warning(f"Session loss limit reached: ${-self.session_realized_pnl:.2f}")
        return False

    return True

def reset_session(self):
    """Reset at start of new trading session."""
    self.session_realized_pnl = 0.0
```

### Phase 3: Wire to Live Trading

**File:** `scripts/run_paper_bot.py`

After each trade completion:
```python
# Record PnL and check session limit
if not balance_manager.record_trade_pnl(trade_pnl):
    logger.error("Session loss limit reached - stopping trading")
    break
```

---

## Files to Modify

| File | Changes | Priority |
|------|---------|----------|
| `research/optimizers/aggressive_grid_search.py` | Add stop_loss_pct to grid (15%, 20%, 30%) | **FIRST** |
| `src/services/balance_manager.py` | Add session loss tracking ($50 limit) | AFTER GRID |
| `src/strategies/enhanced_spike.py` | Add stop-loss logic (if grid shows value) | AFTER GRID |

---

## Expected Grid Search Output

**~70 configs:** 2 offsets × 3 time-stops × 4 stop-losses × 3 market-limits (minus invalid)

| Offset | TS | SL% | MML | $/hr | Trades | Max DD |
|--------|-----|-----|-----|------|--------|--------|
| CURRENT | 180s | None | None | $10.62 | 560 | $81 |
| CURRENT | 180s | 20% | None | ? | ? | ? |
| CURRENT | 180s | None | 2 | ? | ? | ? |
| CURRENT | 180s | 20% | 2 | ? | ? | ? |
| CURRENT | 30s | None | None | $11.61 | 874 | ? |
| CURRENT | 0s | 20% | 2 | ? | ? | ? |
| ... | ... | ... | ... | ... | ... | ... |

**Key questions:**
1. Does % stop-loss reduce max drawdown without hurting $/hr?
2. Does market loss limit (MML=2) provide the expected 18% improvement?
3. Do they combine well together?

---

## Verification

1. **Grid search:** Run on OOS7+OOS8 with stop-loss % variants
2. **Analyze:** Compare $/hr vs max drawdown tradeoff
3. **Session limit:** Add $50 limit to balance_manager after grid results
