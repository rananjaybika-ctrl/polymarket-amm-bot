#!/usr/bin/env python3
"""
Simple Hedger Bot Runner

Runs the Simple Hedger v1 strategy for BTC 15-minute markets.

Usage:
    python scripts/run_simple_hedger.py
    python scripts/run_simple_hedger.py --live  # Real trading
    python scripts/run_simple_hedger.py --duration 60  # Run for 60 minutes
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.services.market_finder import MarketFinder
from src.services.market_rotator import MarketRotator
from src.services.pair_analyzer import PairAnalyzer
from src.strategies.simple_hedger import SimpleHedgerStrategy, Phase
from src.services.paper_trading import PaperTradingEngine, FillType
from src.services.live_trading import LiveTradingEngine
from src.models.position import Position

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


class SimpleHedgerBot:
    """
    Minimal bot runner for Simple Hedger strategy.

    One market at a time, one cycle per market.
    """

    def __init__(
        self,
        live_mode: bool = False,
        initial_balance: float = 100.0,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
    ):
        self.live_mode = live_mode
        self.initial_balance = initial_balance
        self.session_start_utc = session_start_utc
        self.session_end_utc = session_end_utc

        self._config: Optional[Config] = None
        self._client: Optional[PolymarketClient] = None
        self._finder: Optional[MarketFinder] = None
        self._rotator: Optional[MarketRotator] = None
        self._analyzer: Optional[PairAnalyzer] = None
        self._engine = None  # Paper or Live engine

        self._strategy: Optional[SimpleHedgerStrategy] = None
        self._current_market = None
        self._market_start_time: float = 0

        self._running = False
        self._shutdown_event = asyncio.Event()

        # Track pending orders for cancel/replace
        self._pending_order_id: Optional[str] = None

        # Paper trading: pending order tuple (side, price, size)
        self._pending_paper_order: Optional[tuple] = None

    @property
    def client(self):
        """Expose client for web server access."""
        return self._client

    def graceful_stop(self):
        """Request graceful stop."""
        self._shutdown_event.set()

    def request_stop(self):
        """Request stop (alias for graceful_stop)."""
        self._shutdown_event.set()

    async def emergency_sell_all(self):
        """Emergency sell - for simple hedger just stop."""
        self._shutdown_event.set()
        return {"positions_closed": 0, "total_proceeds": 0.0, "details": []}

    async def initialize(self):
        """Initialize all components."""
        logger.info(f"Initializing Simple Hedger Bot ({'LIVE' if self.live_mode else 'PAPER'} mode)")

        self._config = Config()
        self._client = PolymarketClient(self._config)
        await self._client.connect()

        self._finder = MarketFinder()
        self._analyzer = PairAnalyzer(self._client)

        # Use continuous mode for ongoing trading
        # Pass session time window to allow market selection within bounds
        self._rotator = MarketRotator(
            finder=self._finder,
            continuous=True,
            market_window_minutes=60,
            session_start_utc=self.session_start_utc,
            session_end_utc=self.session_end_utc,
        )

        # Check if markets available - fallback to session mode if needed
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
                continuous=False,  # Session mode
                max_markets=100,
                market_window_minutes=60,
                session_start_utc=self.session_start_utc,
                session_end_utc=self.session_end_utc,
            )

        if self.live_mode:
            self._engine = LiveTradingEngine(
                client=self._client,
                config=self._config,
            )
        else:
            self._engine = PaperTradingEngine(initial_balance=self.initial_balance)

        logger.info("Initialization complete")

    async def run_strategy_cycle(self, market):
        """Run one complete strategy cycle on a market."""
        self._strategy = SimpleHedgerStrategy()
        self._market_start_time = time.time()
        position = Position(market_slug=market.slug)

        logger.info(f"Starting Simple Hedger on {market.slug}")

        poll_interval = 0.5  # 500ms polling

        while not self._shutdown_event.is_set():
            # Check if strategy is done
            if self._strategy.is_done():
                pair_cost = self._strategy.get_pair_cost()
                logger.info(
                    f"CYCLE COMPLETE! Pair cost: ${pair_cost:.4f} | "
                    f"Profit: ${1.0 - pair_cost:.4f} per pair"
                )
                break

            # Check market expiry
            time_remaining = market.time_remaining()
            if time_remaining < 5:
                logger.warning(f"Market expiring, stopping strategy")
                break

            # Get prices via PairAnalyzer
            try:
                opportunity = await self._analyzer.analyze_asymmetric_opportunity(
                    market=market,
                    current_up_size=0,
                    current_down_size=0,
                )
            except Exception as e:
                logger.error(f"Price fetch failed: {e}")
                await asyncio.sleep(poll_interval)
                continue

            if opportunity is None or opportunity.up_ask is None or opportunity.down_ask is None:
                logger.debug("No valid prices, waiting...")
                await asyncio.sleep(poll_interval)
                continue

            # Extract prices
            up_ask = opportunity.up_ask
            down_ask = opportunity.down_ask
            up_bid = opportunity.up_bid or (up_ask * 0.98 if up_ask else 0.48)
            down_bid = opportunity.down_bid or (down_ask * 0.98 if down_ask else 0.48)

            # Get strategy decision
            time_in_market = time.time() - self._market_start_time
            current_time = time.time()

            # Paper trading: check pending order for fill
            if not self.live_mode and self._pending_paper_order:
                state = self._strategy.state
                if state.can_paper_fill(current_time, min_delay=0.5):
                    # Attempt fill simulation
                    fill_type = self._engine._simulate_fill()
                    if fill_type != FillType.NONE:
                        side, price, size = self._pending_paper_order
                        # Partial fill handling
                        if fill_type == FillType.PARTIAL:
                            filled_size = int(size * 0.6)  # 60% partial
                            logger.info(f"PAPER PARTIAL: {filled_size}/{size} {side} @ ${price:.4f}")
                        else:
                            filled_size = size
                            logger.info(f"PAPER FILL: {size} {side} @ ${price:.4f}")
                        self._strategy.on_fill(side, price, filled_size)
                        self._pending_paper_order = None
                        state.paper_order_placed_at = 0
                        state.paper_fill_attempts = 0
                    else:
                        state.paper_fill_attempts += 1
                        if state.paper_fill_attempts % 5 == 0:
                            logger.debug(f"Paper order still pending ({state.paper_fill_attempts} attempts)")

            action = self._strategy.decide(
                time_in_market=time_in_market,
                up_bid=up_bid,
                up_ask=up_ask,
                down_bid=down_bid,
                down_ask=down_ask,
                current_time=current_time,
            )

            # Check for order timeout (needs cancel)
            if self._strategy.should_cancel_pending(current_time):
                if self._pending_order_id:
                    logger.info(f"Order timeout - cancelling {self._pending_order_id[:16]}...")
                    try:
                        if self.live_mode:
                            await self._engine.cancel_order(self._pending_order_id)
                    except Exception as e:
                        logger.warning(f"Cancel failed: {e}")
                    self._pending_order_id = None

            # Execute action if any
            if action:
                side, price, size = action
                logger.info(f"Placing order: {size} {side} @ ${price:.4f}")

                try:
                    if self.live_mode:
                        result = await self._engine.execute_single_side_trade(
                            market=market,
                            side=side,
                            price=price,
                            size=size,
                        )

                        if result.get("success"):
                            filled_price = result.get("filled_price", price)
                            filled_size = result.get("filled_size", size)
                            self._strategy.on_fill(side, filled_price, filled_size)
                            logger.info(f"FILLED: {filled_size} {side} @ ${filled_price:.4f}")
                        else:
                            self._pending_order_id = result.get("order_id")
                    else:
                        # Paper trading - create pending order (competitive simulation)
                        self._pending_paper_order = (side, price, size)
                        self._strategy.state.paper_order_placed_at = current_time
                        self._strategy.state.paper_fill_attempts = 0
                        logger.info(f"PAPER ORDER: {size} {side} @ ${price:.4f} (pending)")

                except Exception as e:
                    logger.error(f"Order failed: {e}")

            # Log status periodically
            state = self._strategy.state
            if int(time_in_market) % 5 == 0:
                logger.debug(
                    f"Status: {state.phase.value} | "
                    f"UP: ${up_ask:.2f} | DOWN: ${down_ask:.2f}"
                )

            await asyncio.sleep(poll_interval)

        return self._strategy.is_done()

    async def run(self, duration_minutes: float = 60):
        """Main run loop."""
        self._running = True
        end_time = time.time() + (duration_minutes * 60)

        logger.info(f"Starting Simple Hedger Bot for {duration_minutes:.1f} minutes")

        # Start the rotator session to get initial market
        if not await self._rotator.start_session():
            logger.error("Failed to start session - no markets available")
            return

        markets_traded = 0
        pairs_completed = 0

        try:
            while self._running and time.time() < end_time:
                if self._shutdown_event.is_set():
                    break

                # Get current market from rotator
                market = self._rotator.current_market

                if not market:
                    logger.info("No market available, waiting 10s...")
                    await asyncio.sleep(10)
                    # Try to rotate to get a new market
                    await self._rotator.rotate()
                    continue

                # Check if market has enough time remaining
                time_remaining = market.time_remaining()
                if time_remaining < 30:
                    logger.info(f"Market {market.slug} expiring ({time_remaining:.0f}s), rotating...")
                    await self._rotator.rotate()
                    continue

                logger.info(
                    f"Trading: {market.slug} | "
                    f"Time remaining: {time_remaining:.0f}s"
                )

                # Run strategy on market
                markets_traded += 1
                success = await self.run_strategy_cycle(market)

                if success:
                    pairs_completed += 1

                # Wait for next market
                logger.info(f"Cycle done. Markets: {markets_traded}, Pairs: {pairs_completed}")

                # Check if we should rotate
                if self._rotator.should_rotate():
                    await self._rotator.rotate()
                else:
                    await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        finally:
            await self.cleanup()

        logger.info(
            f"Simple Hedger finished. "
            f"Markets: {markets_traded}, Pairs: {pairs_completed}"
        )

    async def cleanup(self):
        """Cleanup resources."""
        logger.info("Cleaning up...")
        self._running = False
        if self._finder:
            await self._finder.close()
        if self._client:
            await self._client.disconnect()

    def request_shutdown(self):
        """Request graceful shutdown."""
        logger.info("Shutdown requested")
        self._shutdown_event.set()


async def main():
    parser = argparse.ArgumentParser(description="Simple Hedger Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live trading mode (default: paper)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duration in minutes (default: 60)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=100.0,
        help="Initial balance for paper trading (default: 100)",
    )

    args = parser.parse_args()

    bot = SimpleHedgerBot(
        live_mode=args.live,
        initial_balance=args.balance,
    )

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.request_shutdown)

    await bot.initialize()
    await bot.run(duration_minutes=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
