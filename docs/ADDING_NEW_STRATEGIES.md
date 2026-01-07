# Adding New Trading Strategies to the Web Frontend

When adding a new trading strategy bot to the web frontend, follow this checklist to ensure compatibility with the existing infrastructure.

## Required Bot Interface

Your bot class must implement these methods/properties for web server compatibility:

```python
class YourStrategyBot:
    def __init__(
        self,
        live_mode: bool = False,
        initial_balance: float = 100.0,
        session_start_utc: Optional[datetime] = None,  # REQUIRED for market filtering
        session_end_utc: Optional[datetime] = None,    # REQUIRED for market filtering
        web_callback: Optional[callable] = None,       # REQUIRED for frontend updates
    ):
        self._web_callback = web_callback
        # Track prices for web UI
        self._last_up_price: float = 0.0
        self._last_down_price: float = 0.0
        self._trade_count: int = 0
        self._total_pairs: int = 0
        ...

    @property
    def client(self):
        """Expose PolymarketClient for web server access."""
        return self._client

    def graceful_stop(self):
        """Request graceful stop - sets shutdown event."""
        self._shutdown_event.set()

    def request_stop(self):
        """Alias for graceful_stop."""
        self._shutdown_event.set()

    async def emergency_sell_all(self):
        """Emergency sell all positions. Return dict with results."""
        ...

    async def initialize(self):
        """Initialize all components."""
        ...

    async def run(self, duration_minutes: float):
        """Main run loop."""
        ...

    async def cleanup(self):
        """Cleanup resources."""
        ...

    def _build_web_state(self) -> dict:
        """Build trading state as JSON for web UI."""
        # See SimpleHedgerBot for example implementation
        return {
            "type": "trading_update",
            "strategy": "your_strategy_name",
            "market_slug": market.slug if market else "No market",
            "time_remaining": "5:30",
            "position": {...},
            "metrics": {...},
        }

    def _send_web_update(self) -> None:
        """Send trading state to web UI if callback is set."""
        if self._web_callback:
            try:
                state = self._build_web_state()
                self._web_callback(state)
            except Exception as e:
                logger.warning(f"Failed to send web update: {e}")
```

## Critical: MarketRotator Configuration

The MarketRotator MUST receive session time bounds to find markets correctly:

```python
async def initialize(self):
    self._finder = MarketFinder()

    # MUST pass session times to MarketRotator
    self._rotator = MarketRotator(
        finder=self._finder,
        continuous=True,
        market_window_minutes=60,
        session_start_utc=self.session_start_utc,  # From constructor
        session_end_utc=self.session_end_utc,      # From constructor
    )

    # CRITICAL: Fallback to session mode if no markets found
    if self.session_start_utc and self.session_end_utc:
        window_markets = await self._finder.get_markets_in_time_range(
            start_utc=self.session_start_utc,
            end_utc=self.session_end_utc,
        )
    else:
        window_markets = await self._finder.get_markets_in_window(hours=1.0)

    if not window_markets:
        logger.info("No markets in configured time window, using session mode")
        self._rotator = MarketRotator(
            finder=self._finder,
            continuous=False,  # Session mode fallback
            max_markets=100,
            market_window_minutes=60,
            session_start_utc=self.session_start_utc,
            session_end_utc=self.session_end_utc,
        )
```

## Server Integration (web/server.py)

In the `run_your_strategy_bot()` function:

```python
async def run_your_strategy_bot(config: YourBotConfig, strategy: StrategyState):
    # 1. Normalize times to UTC
    start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
    end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

    # 2. Create web callback for frontend updates
    web_callback = create_web_callback_for_strategy("your_strategy_name")

    # 3. MUST pass session times AND web_callback to bot constructor
    bot = YourStrategyBot(
        live_mode=(config.mode == "live"),
        initial_balance=config.starting_balance,
        session_start_utc=start_dt_utc,    # REQUIRED for market filtering
        session_end_utc=end_dt_utc,        # REQUIRED for market filtering
        web_callback=web_callback,          # REQUIRED for frontend updates
    )

    # 4. Initialize and run
    await bot.initialize()
    await bot.run(duration_minutes=duration_minutes)
```

## Common API Gotchas

| Wrong | Correct |
|-------|---------|
| `PolymarketClient()` | `PolymarketClient(config)` |
| `MarketFinder(client)` | `MarketFinder()` |
| `await finder.initialize()` | (not needed - lazy init) |
| `market.time_remaining_seconds` | `market.time_remaining()` |
| `market.up_ask` / `market.down_ask` | Use `PairAnalyzer.analyze_asymmetric_opportunity()` |
| `SimulationConfig(initial_balance=x)` | `PaperTradingEngine(initial_balance=x)` |
| `rotator.get_active_markets()` | `rotator.current_market` + `rotator.should_rotate()` |

## Getting Prices (CRITICAL)

BTCMarket does NOT have `up_ask`, `down_ask`, `up_bid`, `down_bid` attributes.
You MUST use `PairAnalyzer` to get both orderbooks:

```python
from src.services.pair_analyzer import PairAnalyzer

# In __init__:
self._analyzer: Optional[PairAnalyzer] = None

# In initialize():
self._analyzer = PairAnalyzer(self._client)

# In your trading loop:
opportunity = await self._analyzer.analyze_asymmetric_opportunity(
    market=market,
    current_up_size=0,
    current_down_size=0,
)

if opportunity is None or opportunity.up_ask is None:
    continue  # No valid prices

up_ask = opportunity.up_ask
down_ask = opportunity.down_ask
up_bid = opportunity.up_bid or (up_ask * 0.98)
down_bid = opportunity.down_bid or (down_ask * 0.98)
```

## MarketRotator Usage Pattern

```python
# Start session (gets initial market)
if not await self._rotator.start_session():
    logger.error("No markets available")
    return

# Main loop
while running:
    market = self._rotator.current_market

    if not market:
        await self._rotator.rotate()
        continue

    # Check expiry
    if market.time_remaining() < 30:
        await self._rotator.rotate()
        continue

    # Trade on market...

    # Check rotation
    if self._rotator.should_rotate():
        await self._rotator.rotate()
```

## Fixes Applied to SimpleHedger (2026-01-06)

1. `PolymarketClient()` -> `PolymarketClient(self._config)`
2. Removed `MarketFinder(client)` argument - takes no args
3. Removed non-existent `await finder.initialize()`
4. Fixed `MarketRotator` params - removed `min_time_remaining`, `market_type`
5. Added `session_start_utc`, `session_end_utc` to constructor
6. Pass session times to `MarketRotator`
7. Added fallback from continuous to session mode
8. `market.time_remaining_seconds` -> `market.time_remaining()`
9. `SimulationConfig(initial_balance=x)` -> `PaperTradingEngine(initial_balance=x)`
10. Server: pass `session_start_utc`, `session_end_utc` to bot
11. Added `web_callback` parameter for frontend updates
12. Added `_build_web_state()` method to build trading state JSON
13. Added `_send_web_update()` method to broadcast to frontend
14. Call `_send_web_update()` in strategy loop after getting prices
15. Server: create and pass `web_callback=create_web_callback_for_strategy("simple_hedger")`

## Fixes Applied for Fair Value MM (2026-01-06)

### Python Fixes
1. Add `Tuple` to typing imports in run_paper_bot.py: `from typing import ..., Tuple`
2. Add Telegram mode handler for new strategy in `initialize()`:
   ```python
   elif self.accum_mode == "fair_value_mm":
       mode_label = "Fair Value MM"
       self._telegram.on_graceful_stop_calculus_maker(self._handle_telegram_graceful_stop)
       # ... set others to _noop
   ```
3. Add mode to `accum_mode` validation list if applicable
4. Add mode to CalculusMakerStrategy initialization condition:
   ```python
   if self.accum_mode in ("calculus_maker", "fair_value_mm"):
       self._calculus_strategy = CalculusMakerStrategy(...)
   ```

### Server Fixes (web/server.py)
1. Add `YourStrategyBotConfig(BaseModel)` class with all config fields
2. Add `"your_strategy": StrategyState("your_strategy")` to `strategies` dict
3. Add strategy to `get_status()` response and `running` check
4. Add `@app.post("/api/start/your_strategy")` endpoint
5. Add `run_your_strategy_bot()` async function

### Frontend Fixes (web/static/index.html)
1. Add full card HTML with:
   - Card container: `<div class="mode-card" id="card-your-strategy">`
   - Status badge: `id="badge-your-strategy"`
   - Live data section with position table
   - Config toggle and form fields
   - Action buttons: START, STOP, NUKE

### Frontend Fixes (web/static/app.js)
1. Add to `modes` object:
   ```javascript
   'your-strategy': {
       status: 'stopped',
       running: false,
       config: {},
       liveData: {},
       timeRemaining: 0,
       countdownInterval: null
   },
   ```
2. Add to `clearAllPositionDisplays()` array
3. Add to config toggles forEach array
4. Add `setupModeButtons('your-strategy')`
5. Add to `setDefaultDatetimes()` forEach array
6. Add `getYourStrategyConfig()` function
7. Update `handleStart()` with new mode case:
   ```javascript
   } else if (modeName === 'your-strategy') {
       config = getYourStrategyConfig();
       endpoint = '/api/start/your_strategy';
   }
   ```
8. Update `handleStop()` strategy mapping:
   ```javascript
   else if (modeName === 'your-strategy') strategy = 'your_strategy';
   ```
9. Update `handleNuke()` strategy mapping (same as handleStop)
10. Update `routeMessage()` to include new strategy status
11. Update `fetchStatus()` to include new strategy status

### Naming Convention
- Frontend uses **hyphens**: `fair-value-mm`, `your-strategy`
- Backend uses **underscores**: `fair_value_mm`, `your_strategy`
- Convert in JS: `modeName.replace('-', '_')` or explicit mapping
