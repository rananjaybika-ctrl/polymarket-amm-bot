# Integration Plan: Path 1 (AGGRESSIVE) & Path 2 (CONTRARIAN)

**Created:** January 25, 2026
**Status:** Ready for Implementation

---

## Executive Summary

This plan integrates two validated trading strategies into the production architecture:

| Strategy | Status | Performance | Implementation Effort |
|----------|--------|-------------|----------------------|
| **Path 1 (AGGRESSIVE)** | Validated | $16.72/hr @50sh, 72.4% dir acc | Medium - Strategy exists |
| **Path 2 (CONTRARIAN)** | Validated | 43.4% WR, +7.9pp edge | Medium - New strategy class |

---

## Current Architecture Analysis

### Strategy Registration Pattern

Each strategy requires 5 components:

```
1. Pydantic Config Model (server.py)     -> AggressiveBotConfig, ContrarianBotConfig
2. Strategy State Entry (server.py)       -> strategies["aggressive"], strategies["contrarian"]
3. Runner Function (server.py)            -> run_aggressive_bot(), run_contrarian_bot()
4. PaperTradingBot Factory (run_paper_bot.py) -> from_aggressive_config(), from_contrarian_config()
5. Frontend Card (index.html + app.js)    -> HTML + JavaScript handlers
```

### Existing Strategies (To Remove)

| Strategy | Files | Reason |
|----------|-------|--------|
| calculus_maker | server.py, index.html, app.js | Superseded by Path 1/2 |
| fair_value_mm | server.py, index.html, app.js | Superseded by Path 1/2 |
| spread_capture | server.py, index.html, app.js | Superseded by Path 1/2 |

**Note:** "VW" strategy not found in codebase. Clarify if this refers to a planned or external strategy.

---

## Part 1: Add Path 1 (AGGRESSIVE) Strategy

### 1.1 Config Model (server.py)

```python
class AggressiveBotConfig(BaseModel):
    """Configuration for AGGRESSIVE (Path 1) strategy from web UI.

    Spike detection + full hedge with TIME120s_SKIP config.
    Performance: ~$9.00/hr @50sh (cross-validated 157.4h).
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str
    end_datetime: str
    starting_balance: float = 500.0

    # Path 1 specific parameters (from research/strategies/AGGRESSIVE.md)
    threshold_method: str = "ou"           # OU adaptive threshold
    zscore_method: str = "ewma"            # EWMA z-score (adapts, doesn't drift)
    lookback_ms: int = 1200                # 1200ms lookback (72 ticks at 60Hz)
    time_stop_seconds: float = 120.0       # 120s time-stop (optimized from 180s)
    min_time_remaining: float = 180.0      # time_stop + 60s buffer
    use_cycling: bool = True               # Re-enter after exit
    z_lo: float = 0.0                      # Z-zone lower bound
    z_hi: float = 1.5                      # Z-zone upper bound

    # TIME120s_SKIP parameters
    skip_high_entry: bool = True           # Skip entries >= $0.90
    high_entry_threshold: float = 0.90     # Turkey problem cutoff

    # Position sizing
    base_size: int = 50                    # Shares per trade (validated @50)
    target_shares: int = 50                # Target per market

    # Risk management
    max_share_price: float = 0.95
    max_daily_loss: float = 0.0            # 0 = disabled
```

### 1.2 Strategy State Entry (server.py)

```python
strategies = {
    "aggressive": StrategyState("aggressive"),    # Path 1: AGGRESSIVE
    "contrarian": StrategyState("contrarian"),    # Path 2: CONTRARIAN
    # Remove: calculus_maker, fair_value_mm, spread_capture
}
```

### 1.3 Runner Function (server.py)

```python
async def run_aggressive_bot(config: AggressiveBotConfig, strategy: StrategyState):
    """Run AGGRESSIVE (Path 1) trading bot.

    Uses EnhancedSpikeStrategy with:
    - OU adaptive threshold for spike detection
    - EWMA z-score filtering (z=[0, 1.5])
    - 180s time-stop (not price-stop)
    - Full hedge on loser side
    """
    restart_configs["aggressive"] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        # Wait until start
        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0
        web_callback = create_web_callback_for_strategy("aggressive")

        # Create AGGRESSIVE bot
        bot = PaperTradingBot.from_aggressive_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        await bot.initialize()
        await bot.run(duration_minutes=duration_minutes)

        # Normal completion
        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("aggressive", None)
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("aggressive", None)
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("aggressive", None)
        await broadcast_status()
```

### 1.4 PaperTradingBot Factory (run_paper_bot.py)

```python
@classmethod
def from_aggressive_config(
    cls,
    config: Dict[str, Any],
    web_callback=None,
    session_start_utc=None,
    session_end_utc=None,
    trading_mode: str = "paper",
) -> "PaperTradingBot":
    """Create bot configured for AGGRESSIVE (Path 1) strategy."""
    from src.strategies.enhanced_spike import EnhancedSpikeStrategy
    from src.strategies.ou_volatility import OUAdaptiveThreshold
    from src.services.volatility_tracker import create_aggressive_tracker

    # Create z-score tracker (EWMA method, z=[0, 1.5])
    zscore_tracker = create_aggressive_tracker()

    # Create OU adaptive threshold
    ou_threshold = OUAdaptiveThreshold()

    # Create EnhancedSpikeStrategy with Path 1 config
    strategy = EnhancedSpikeStrategy(
        base_size=config.get("base_size", 50),
        target_shares=config.get("target_shares", 50),
        spike_lookback=config.get("lookback_ms", 1200) // 20,  # Convert ms to ticks at 50Hz
        enable_cycling=config.get("use_cycling", True),
        stop_loss_pct=None,  # NO price-stop for Path 1
        ou_adaptive_threshold=ou_threshold,
        zscore_tracker=zscore_tracker,
        zscore_lo=config.get("z_lo", 0.0),
        zscore_hi=config.get("z_hi", 1.5),
    )

    return cls(
        strategy=strategy,
        time_stop_seconds=config.get("time_stop_seconds", 180.0),
        initial_balance=config.get("starting_balance", 500.0),
        web_callback=web_callback,
        session_start_utc=session_start_utc,
        session_end_utc=session_end_utc,
        trading_mode=trading_mode,
    )
```

### 1.5 API Endpoints (server.py)

```python
@app.post("/api/start/aggressive")
async def start_aggressive(
    config: AggressiveBotConfig,
    username: str = Depends(verify_credentials)
):
    """Start AGGRESSIVE (Path 1) strategy."""
    strategy = strategies["aggressive"]

    if strategy.status["running"]:
        raise HTTPException(400, "AGGRESSIVE already running")

    if is_kill_switch_active():
        raise HTTPException(400, "Kill switch active")

    strategy.status = {
        "running": True,
        "strategy": "aggressive",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
    }

    strategy.task = asyncio.create_task(run_aggressive_bot(config, strategy))
    await broadcast_status()

    return {"success": True, "message": "AGGRESSIVE started"}
```

---

## Part 2: Add Path 2 (CONTRARIAN) Strategy

### 2.1 Config Model (server.py)

```python
class ContrarianBotConfig(BaseModel):
    """Configuration for CONTRARIAN (Path 2) strategy from web UI.

    Mean-reversion betting against BTC moves.
    Performance: 43.4% WR, +7.9pp edge, hold to resolution.
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str
    end_datetime: str
    starting_balance: float = 500.0

    # Path 2 specific parameters (from research/strategies/CONTRARIAN.md)
    pullback_threshold: float = 0.0001     # 0.01% absolute pullback
    retracement_min: float = 0.30          # Pullback >= 30% of peak move
    entry_price_min: float = 0.20          # Skip entries < $0.20
    min_delay_seconds: int = 60            # Wait 60s into window

    # Vol gate parameters
    vol_gate_k: float = 0.5                # Trade when vol >= 50% of recent avg
    vol_gate_halflife: int = 50            # ~12.5 hours lookback
    z_threshold: float = 0.5               # Z-score >= 0.5 required

    # Position sizing
    shares_per_trade: int = 2500           # Validated size
    entry_price_target: float = 0.30       # Buy at ~$0.30

    # Exit: Hold to resolution (no stops)
    # Risk: $0.30/share, Reward: $0.70/share = 2.33:1 R:R

    max_daily_loss: float = 0.0
```

### 2.2 New Strategy Class (src/strategies/contrarian.py)

```python
"""
CONTRARIAN Strategy (Path 2) - Mean Reversion at 15-minute Scale

Buy the cheap side (~$0.30) when BTC shows reversal confirmation.
Hold to resolution for 2.33:1 reward-to-risk.

Validated Performance:
- Win Rate: 43.4%
- Edge: +7.9pp over baseline
- Breakeven: 30% WR (well above)
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ContrarianState:
    """State tracking for contrarian strategy."""
    window_start_time: float = 0.0
    btc_open_price: float = 0.0
    peak_move_pct: float = 0.0
    peak_direction: Optional[str] = None
    entry_triggered: bool = False
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: float = 0.0


class AdaptiveEWMAGate:
    """Self-calibrating volatility gate."""

    def __init__(self, k: float = 0.5, halflife_windows: int = 50):
        self.k = k
        self.alpha = 1 - 0.5 ** (1 / halflife_windows)
        self.vol_ema: Optional[float] = None

    def update_and_check(self, pre_vol: float) -> bool:
        """Update EMA and check if trading allowed."""
        if self.vol_ema is None:
            self.vol_ema = pre_vol
            return True

        ratio = pre_vol / self.vol_ema
        allowed = ratio >= self.k

        # Update AFTER check (no lookahead)
        self.vol_ema = self.alpha * pre_vol + (1 - self.alpha) * self.vol_ema
        return allowed


class ContrarianStrategy:
    """
    CONTRARIAN Strategy - Bet against BTC directional moves.

    Entry Logic:
    1. Wait min_delay_seconds from window open
    2. Detect reversal: pullback >= pullback_threshold
    3. Apply filters: retracement_frac >= 0.30, entry_price >= $0.20
    4. Check z-score >= z_threshold
    5. Enter contrarian direction at ~$0.30

    Exit: Hold to resolution (no stops)
    """

    def __init__(
        self,
        pullback_threshold: float = 0.0001,
        retracement_min: float = 0.30,
        entry_price_min: float = 0.20,
        min_delay_seconds: int = 60,
        z_threshold: float = 0.5,
        shares_per_trade: int = 2500,
    ):
        self.pullback_threshold = pullback_threshold
        self.retracement_min = retracement_min
        self.entry_price_min = entry_price_min
        self.min_delay_seconds = min_delay_seconds
        self.z_threshold = z_threshold
        self.shares_per_trade = shares_per_trade

        self.vol_gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)
        self.state = ContrarianState()

        # Tracking
        self._price_history: list = []
        self._windows_gated: int = 0
        self._entries_made: int = 0

    def on_window_start(self, btc_price: float, pre_vol: float, timestamp: float):
        """Called at start of each 15-minute window."""
        # Check vol gate
        if not self.vol_gate.update_and_check(pre_vol):
            self._windows_gated += 1
            self.state = ContrarianState()  # Reset state
            return False

        # Initialize for new window
        self.state = ContrarianState(
            window_start_time=timestamp,
            btc_open_price=btc_price,
        )
        self._price_history = [btc_price]
        return True

    def update(
        self,
        btc_price: float,
        timestamp: float,
        cheap_price: float,  # Current price of cheap side (~$0.30-0.40)
    ) -> Optional[Tuple[str, float, int]]:
        """
        Update with new BTC price and return entry signal if any.

        Returns:
            (side, price, size) if entry triggered, else None
        """
        if self.state.entry_triggered:
            return None  # Already entered this window

        elapsed = timestamp - self.state.window_start_time

        # Must wait min_delay_seconds
        if elapsed < self.min_delay_seconds:
            return None

        # Track price history
        self._price_history.append(btc_price)

        # Calculate move from open
        move_pct = (btc_price - self.state.btc_open_price) / self.state.btc_open_price
        abs_move = abs(move_pct)

        # Update peak
        if abs_move > abs(self.state.peak_move_pct):
            self.state.peak_move_pct = move_pct
            self.state.peak_direction = "UP" if move_pct > 0 else "DOWN"

        # Check for reversal (pullback from peak)
        if self.state.peak_direction is None:
            return None

        if self.state.peak_direction == "UP":
            pullback = self.state.peak_move_pct - move_pct
        else:
            pullback = move_pct - self.state.peak_move_pct

        # Must have minimum absolute pullback
        if pullback < self.pullback_threshold:
            return None

        # Calculate retracement fraction
        if abs(self.state.peak_move_pct) < 0.0001:
            return None
        retracement_frac = pullback / abs(self.state.peak_move_pct)

        # FILTER: Retracement >= 0.30
        if retracement_frac < self.retracement_min:
            return None

        # FILTER: Entry price >= $0.20
        if cheap_price < self.entry_price_min:
            return None

        # Calculate z-score
        vol_per_s = self._estimate_volatility_per_second()
        if vol_per_s < 0.00001:
            return None
        z_score = abs_move / (vol_per_s * math.sqrt(elapsed))

        # FILTER: Z-score >= threshold
        if z_score < self.z_threshold:
            return None

        # ENTRY: Bet against the move (contrarian)
        entry_side = "DOWN" if self.state.peak_direction == "UP" else "UP"

        self.state.entry_triggered = True
        self.state.entry_side = entry_side
        self.state.entry_price = cheap_price
        self.state.entry_time = timestamp
        self._entries_made += 1

        logger.info(
            f"[CONTRARIAN] Entry: {entry_side} @ ${cheap_price:.3f}, "
            f"peak_move={self.state.peak_move_pct:.4%}, retrace={retracement_frac:.2f}, "
            f"z={z_score:.2f}"
        )

        return (entry_side, cheap_price, self.shares_per_trade)

    def _estimate_volatility_per_second(self) -> float:
        """Estimate per-second volatility from recent prices."""
        if len(self._price_history) < 10:
            return 0.0001  # Default

        returns = []
        for i in range(1, len(self._price_history)):
            r = (self._price_history[i] - self._price_history[i-1]) / self._price_history[i-1]
            returns.append(r)

        if not returns:
            return 0.0001

        variance = sum(r**2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def get_stats(self) -> dict:
        """Get strategy statistics."""
        return {
            "windows_gated": self._windows_gated,
            "entries_made": self._entries_made,
            "vol_ema": self.vol_gate.vol_ema,
        }
```

### 2.3 Runner Function (server.py)

```python
async def run_contrarian_bot(config: ContrarianBotConfig, strategy: StrategyState):
    """Run CONTRARIAN (Path 2) trading bot.

    Mean-reversion strategy betting against BTC moves.
    Hold to resolution for 2.33:1 R:R.
    """
    restart_configs["contrarian"] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0
        web_callback = create_web_callback_for_strategy("contrarian")

        bot = PaperTradingBot.from_contrarian_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        await bot.initialize()
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("contrarian", None)
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("contrarian", None)
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("contrarian", None)
        await broadcast_status()
```

---

## Part 3: Frontend Integration

### 3.1 HTML Cards (index.html)

Add two new cards for AGGRESSIVE and CONTRARIAN, following the existing pattern.
Remove cards for: calculus_maker, fair_value_mm, spread_capture.

**AGGRESSIVE Card Parameters:**
- Mode (Paper/Live toggle)
- Start/End datetime
- Balance
- Lookback (ms): 1200
- Time Stop (s): 180
- Z-Lo: 0.0
- Z-Hi: 1.5
- Base Size: 50
- Cycling: checkbox

**CONTRARIAN Card Parameters:**
- Mode (Paper/Live toggle)
- Start/End datetime
- Balance
- Pullback: 0.0001
- Retracement Min: 0.30
- Entry Price Min: 0.20
- Delay (s): 60
- Z Threshold: 0.5
- Shares: 2500

### 3.2 JavaScript Handlers (app.js)

Update `modes` object:
```javascript
const modes = {
    'aggressive': { status: 'stopped', running: false, config: {}, liveData: {} },
    'contrarian': { status: 'stopped', running: false, config: {}, liveData: {} },
};
```

Add config collectors:
```javascript
function getAggressiveConfig() {
    return {
        mode: document.querySelector('input[name="aggressive_mode"]:checked').value,
        start_datetime: document.getElementById('aggressive-start').value,
        end_datetime: document.getElementById('aggressive-end').value,
        starting_balance: parseFloat(document.getElementById('aggressive-balance-input').value),
        lookback_ms: parseInt(document.getElementById('aggressive-lookback').value),
        time_stop_seconds: parseFloat(document.getElementById('aggressive-timestop').value),
        z_lo: parseFloat(document.getElementById('aggressive-z-lo').value),
        z_hi: parseFloat(document.getElementById('aggressive-z-hi').value),
        base_size: parseInt(document.getElementById('aggressive-size').value),
        use_cycling: document.getElementById('aggressive-cycling').checked,
    };
}

function getContrarianConfig() {
    return {
        mode: document.querySelector('input[name="contrarian_mode"]:checked').value,
        start_datetime: document.getElementById('contrarian-start').value,
        end_datetime: document.getElementById('contrarian-end').value,
        starting_balance: parseFloat(document.getElementById('contrarian-balance-input').value),
        pullback_threshold: parseFloat(document.getElementById('contrarian-pullback').value),
        retracement_min: parseFloat(document.getElementById('contrarian-retracement').value),
        entry_price_min: parseFloat(document.getElementById('contrarian-entry-min').value),
        min_delay_seconds: parseInt(document.getElementById('contrarian-delay').value),
        z_threshold: parseFloat(document.getElementById('contrarian-z-threshold').value),
        shares_per_trade: parseInt(document.getElementById('contrarian-shares').value),
    };
}
```

---

## Part 4: Remove Old Strategies

### Files to Modify

1. **web/server.py:**
   - Remove: `CalculusMakerBotConfig`, `FairValueMMBotConfig`, `SpreadCaptureBotConfig`
   - Remove: strategy state entries for calculus_maker, fair_value_mm, spread_capture
   - Remove: `run_calculus_bot()`, `run_fair_value_mm_bot()`, `run_spread_capture_bot()`
   - Remove: API endpoints for old strategies

2. **web/static/index.html:**
   - Remove: Cards for calculus_maker, fair_value_mm, spread_capture

3. **web/static/app.js:**
   - Remove: modes entries for old strategies
   - Remove: Config collectors for old strategies
   - Remove: Event handlers for old strategies

4. **scripts/run_paper_bot.py:**
   - Remove: `from_calculus_config()`, `from_fair_value_mm_config()`, `from_spread_capture_config()`
   - Keep: Core PaperTradingBot class (used by new strategies)

### Files to Keep (Core Infrastructure)

- `src/strategies/enhanced_spike.py` - Used by AGGRESSIVE
- `src/services/volatility_tracker.py` - LiveZScoreTracker
- `src/strategies/ou_volatility.py` - OUAdaptiveThreshold
- `src/services/paper_trading.py` - Paper trading engine
- `src/bots/live_trading_engine.py` - Live trading infrastructure

---

## Part 5: Implementation Order

### Phase 1: Add New Strategies (Non-Breaking)

1. Add `AggressiveBotConfig` to server.py
2. Add `ContrarianBotConfig` to server.py
3. Add strategy state entries
4. Add runner functions
5. Add API endpoints
6. Add factory methods to run_paper_bot.py
7. Create `src/strategies/contrarian.py`
8. Add frontend cards (keep old cards for now)

### Phase 2: Test New Strategies

1. Paper trade AGGRESSIVE
2. Paper trade CONTRARIAN
3. Verify WebSocket updates work
4. Verify live data displays correctly

### Phase 3: Remove Old Strategies

1. Remove old config models
2. Remove old runners
3. Remove old endpoints
4. Remove old frontend cards
5. Clean up unused imports

---

## Validation Checklist

Before going live, verify:

- [ ] AGGRESSIVE paper trades correctly
- [ ] AGGRESSIVE shows live z-score in UI
- [ ] AGGRESSIVE time-stop triggers at 180s
- [ ] CONTRARIAN paper trades correctly
- [ ] CONTRARIAN vol gate filters ~35% of windows
- [ ] CONTRARIAN holds to resolution (no early exit)
- [ ] WebSocket broadcasts work for both strategies
- [ ] Kill switch prevents both strategies from starting
- [ ] Auto-restart config cleared on stop/error

---

## Performance Expectations

| Metric | AGGRESSIVE | CONTRARIAN |
|--------|------------|------------|
| Hourly Rate | $16.72/hr @50sh | $12/hr @2500sh |
| Direction Accuracy | 72.4% | 43.4% WR |
| Edge | - | +7.9pp |
| Risk/Reward | Full hedge | 2.33:1 R:R |
| Stop Type | 180s time | None (resolution) |

---

## Files Summary

### New Files to Create

1. `src/strategies/contrarian.py` - ContrarianStrategy class

### Files to Modify

1. `web/server.py` - Config models, runners, endpoints
2. `web/static/index.html` - Frontend cards
3. `web/static/app.js` - JavaScript handlers
4. `scripts/run_paper_bot.py` - Factory methods

### Files to Remove (After Testing)

None - just remove code blocks within files listed above.

---

*Integration plan for Path 1 (AGGRESSIVE) and Path 2 (CONTRARIAN) strategies.*
