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
    ):
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

    # 2. MUST pass session times to bot constructor
    bot = YourStrategyBot(
        live_mode=(config.mode == "live"),
        initial_balance=config.starting_balance,
        session_start_utc=start_dt_utc,    # REQUIRED
        session_end_utc=end_dt_utc,        # REQUIRED
    )

    # 3. Initialize and run
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
