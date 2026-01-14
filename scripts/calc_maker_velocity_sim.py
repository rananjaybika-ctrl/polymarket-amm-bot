#!/usr/bin/env python3
"""
Calc Maker Velocity Effectiveness Simulator

Tests the effectiveness of velocity-based entry/hedge timing using real-time data.
Records hypothetical trading activity WITHOUT placing real orders.

Architecture:
- Market Detection: REST API (MarketFinder) - finds active BTC 15-min markets
- Orderbook Data: WebSocket (WebSocketClient) - real-time book updates
- Price Feed: WebSocket (BinanceClient) - real-time BTC price for velocity calculation

Key Metrics Tracked:
- Entry velocity decisions (SKIP vs ENTER)
- Hedge velocity decisions (LET_IT_RIDE vs HEDGE_NOW)
- Price improvement from velocity timing
- Pair costs and profit per cycle

Usage:
    python scripts/calc_maker_velocity_sim.py --hours 8 --output research/
    python scripts/calc_maker_velocity_sim.py --hours 0.1  # Quick 6-min test
"""

import argparse
import asyncio
import csv
import sys
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List
import time
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder
from src.services.trend_detector import TrendDetector
from src.models.market import BTCMarket
from src.strategies.calculus_maker import CalculusMakerStrategy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS (matching calculus_maker.py)
# =============================================================================
VELOCITY_PULL_THRESHOLD = 0.05  # bps/sec - threshold for velocity decisions
MAX_HEDGE_WAIT_SECS = 120.0     # Force hedge after this time
GATE_DURATION = 5.0             # Wait 5s at market open
FILL_TIMEOUT = 60.0             # Timeout for fill simulation (was 30s)
FILL_PROB_BASE = 0.02           # Base fill probability per 100ms when close to ask
MARKET_DURATION = 900           # 15-minute markets in seconds

# NOTE: Entry/hedge offset now uses dynamic get_mispricing_threshold() from strategy
# instead of fixed offset - matches actual calculus_maker.py behavior


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class EntryDecision:
    """Records an entry velocity decision."""
    timestamp: float
    market_slug: str
    expensive_side: str          # "UP" or "DOWN"
    velocity_bps: float          # Velocity at decision time
    decision: str                # "ENTER_NOW" or "SKIP_WAITING"
    raw_up_ask: float            # Market prices at decision
    raw_down_ask: float
    up_bid: float
    down_bid: float
    skipped_price: Optional[float] = None  # Price we would have paid
    actual_entry_price: Optional[float] = None  # What we actually paid
    time_to_fill_ms: Optional[float] = None


@dataclass
class HedgeDecision:
    """Records a hedge velocity decision."""
    timestamp: float
    market_slug: str
    hedge_side: str              # "UP" or "DOWN"
    velocity_bps: float
    time_since_entry_fill: float # Seconds since entry filled
    decision: str                # "HEDGE_NOW" or "LET_IT_RIDE"
    reason: str                  # Decision reason from should_hedge_now()
    immediate_hedge_price: float # Price if hedged immediately after entry
    current_hedge_price: float   # Price at this decision point
    actual_hedge_price: Optional[float] = None  # What we actually paid


@dataclass
class SimulatedFill:
    """Records a simulated fill."""
    side: str
    price: float
    filled_at: float
    time_to_fill_ms: float


@dataclass
class VelocitySample:
    """Records velocity at a point in time for threshold analysis."""
    timestamp: float
    market_slug: str
    btc_price: float
    velocity_bps: float
    up_ask: float
    down_ask: float
    up_bid: float
    down_bid: float
    expensive_side: str  # Which side is more expensive at this moment


@dataclass
class CompletedCycle:
    """Records one complete entry+hedge cycle."""
    cycle_id: int
    market_slug: str
    timestamp: float
    # Entry
    entry_side: str
    entry_price: float
    entry_size: int              # Position size (from dynamic sizing)
    entry_decisions_count: int   # How many SKIPs before ENTER
    entry_velocity_at_fill: float
    first_skipped_price: float   # Price at first skip (baseline)
    entry_improvement_bps: float # (first_skipped - actual) * 10000
    time_to_entry_fill_ms: float
    entry_threshold: float       # Dynamic threshold at entry time
    # Hedge
    hedge_side: str
    hedge_price: float
    hedge_size: int              # Position size (from dynamic sizing)
    hedge_decisions_count: int   # How many LET_RIDEs before HEDGE
    hedge_velocity_at_fill: float
    hedge_reason: str            # "reversal" or "force_timeout"
    immediate_hedge_price: float # What we would have paid immediately
    hedge_improvement_bps: float # (immediate - actual) * 10000
    time_to_hedge_fill_ms: float
    hedge_threshold: float       # Dynamic threshold at hedge time
    # Result
    pair_cost: float
    profit: float                # 1.0 - pair_cost
    profit_dollars: float        # profit * size (actual P&L)
    total_time_ms: float         # Entry to hedge completion


# =============================================================================
# SIMULATOR CLASS
# =============================================================================

class CalcMakerVelocitySim:
    """
    Simulates calc maker with velocity timing, tracks effectiveness.

    Uses real Polymarket orderbook + Binance prices, simulates fills.
    """

    def __init__(self, output_dir: str = "research/"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Strategy
        self.strategy = CalculusMakerStrategy()

        # Clients
        self.binance_client: Optional[BinanceClient] = None
        self.ws_client: Optional[WebSocketClient] = None
        self.trend_detector: Optional[TrendDetector] = None
        self.market_finder: Optional[MarketFinder] = None

        # Current market
        self.current_market: Optional[BTCMarket] = None
        self.strike_price: float = 0.0

        # Orderbook cache (updated by WebSocket)
        self._up_book: Optional[BookUpdate] = None
        self._down_book: Optional[BookUpdate] = None

        # Tracking
        self.entry_decisions: List[EntryDecision] = []
        self.hedge_decisions: List[HedgeDecision] = []
        self.completed_cycles: List[CompletedCycle] = []
        self.velocity_samples: List[VelocitySample] = []  # Time series for threshold analysis
        self._last_sample_time: float = 0.0

        # State
        self._cycle_count: int = 0
        self._in_cycle: bool = False
        self._ws_task: Optional[asyncio.Task] = None

        # Error tracking: unhedged positions
        self.unhedged_entries: List[Dict[str, Any]] = []  # Track entry fills without hedge

        # Incremental save - generate timestamp once at start
        self._run_timestamp: str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._cycles_csv_path: Optional[Path] = None
        self._last_saved_cycle: int = 0  # Track which cycles have been saved

    def _on_book_update(self, update: BookUpdate):
        """Callback for WebSocket book updates."""
        if not self.current_market:
            return

        if update.token_id == self.current_market.up_token_id:
            self._up_book = update
        elif update.token_id == self.current_market.down_token_id:
            self._down_book = update

    def _get_velocity(self) -> float:
        """Get current velocity from trend detector."""
        if not self.trend_detector:
            return 0.0
        signal = self.trend_detector.get_trend_signal()
        return signal.velocity_bps if signal else 0.0

    def _get_orderbook_prices(self) -> tuple[float, float, float, float]:
        """Get current orderbook prices (up_ask, down_ask, up_bid, down_bid)."""
        up_ask = self._up_book.best_ask if self._up_book and self._up_book.best_ask is not None else 0.50
        down_ask = self._down_book.best_ask if self._down_book and self._down_book.best_ask is not None else 0.50
        up_bid = self._up_book.best_bid if self._up_book and self._up_book.best_bid is not None else 0.49
        down_bid = self._down_book.best_bid if self._down_book and self._down_book.best_bid is not None else 0.49
        return up_ask, down_ask, up_bid, down_bid

    def _record_velocity_sample(self):
        """Record a velocity sample for threshold analysis (every 1 second)."""
        now = time.time()
        if now - self._last_sample_time < 1.0:
            return  # Only sample once per second

        self._last_sample_time = now

        if not self.current_market or not self.binance_client:
            return

        up_ask, down_ask, up_bid, down_bid = self._get_orderbook_prices()
        velocity = self._get_velocity()
        expensive_side = "UP" if up_ask >= down_ask else "DOWN"

        self.velocity_samples.append(VelocitySample(
            timestamp=now,
            market_slug=self.current_market.slug,
            btc_price=self.binance_client.current_price,
            velocity_bps=velocity,
            up_ask=up_ask,
            down_ask=down_ask,
            up_bid=up_bid,
            down_bid=down_bid,
            expensive_side=expensive_side,
        ))

    def _save_cycle_incremental(self, cycle: CompletedCycle):
        """
        Save a completed cycle to CSV immediately (incremental save).
        This prevents data loss if the script crashes or disconnects.
        """
        # Initialize CSV file on first cycle
        if self._cycles_csv_path is None:
            self._cycles_csv_path = self.output_dir / f"calc_velocity_sim_{self._run_timestamp}.csv"
            with open(self._cycles_csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'cycle_id', 'market_slug', 'timestamp',
                    'entry_side', 'entry_price', 'entry_size', 'entry_threshold',
                    'entry_decisions', 'entry_improvement_bps',
                    'first_skipped_price', 'time_to_entry_fill_ms',
                    'hedge_side', 'hedge_price', 'hedge_size', 'hedge_threshold',
                    'hedge_decisions', 'hedge_improvement_bps',
                    'hedge_reason', 'immediate_hedge_price', 'time_to_hedge_fill_ms',
                    'pair_cost', 'profit', 'profit_dollars', 'total_time_ms'
                ])
            logger.info(f"[SAVE] Created incremental CSV: {self._cycles_csv_path}")

        # Append this cycle
        with open(self._cycles_csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                cycle.cycle_id, cycle.market_slug, cycle.timestamp,
                cycle.entry_side, cycle.entry_price, cycle.entry_size, cycle.entry_threshold,
                cycle.entry_decisions_count, cycle.entry_improvement_bps,
                cycle.first_skipped_price, cycle.time_to_entry_fill_ms,
                cycle.hedge_side, cycle.hedge_price, cycle.hedge_size, cycle.hedge_threshold,
                cycle.hedge_decisions_count, cycle.hedge_improvement_bps,
                cycle.hedge_reason, cycle.immediate_hedge_price, cycle.time_to_hedge_fill_ms,
                cycle.pair_cost, cycle.profit, cycle.profit_dollars, cycle.total_time_ms
            ])

        self._last_saved_cycle = cycle.cycle_id

    async def initialize(self):
        """Initialize all clients."""
        logger.info("Initializing Calc Maker Velocity Simulator...")

        # Binance client
        self.binance_client = BinanceClient()
        await self.binance_client.connect()

        # Wait for Binance connection
        logger.info("Waiting for Binance WebSocket...")
        for _ in range(100):
            if self.binance_client.is_connected and self.binance_client.current_price > 0:
                break
            await asyncio.sleep(0.1)

        if not self.binance_client.is_connected or self.binance_client.current_price <= 0:
            raise RuntimeError("Failed to connect to Binance WebSocket")

        logger.info(f"Binance connected! BTC: ${self.binance_client.current_price:,.2f}")

        # Trend detector
        self.trend_detector = TrendDetector(self.binance_client)

        # Market finder
        self.market_finder = MarketFinder()

        # Polymarket WebSocket
        self.ws_client = WebSocketClient(auto_reconnect=True)
        self.ws_client.on_book_update(self._on_book_update)

        logger.info("Simulator initialized.")

    async def find_active_market(self) -> Optional[BTCMarket]:
        """Find the current active BTC 15-min market."""
        market = await self.market_finder.get_current_market()
        if market and market.time_remaining() > 60:
            return market

        markets = await self.market_finder.get_current_and_upcoming_markets(count=3)
        for m in markets:
            if m.is_active() and m.time_remaining() > 60:
                return m

        return markets[0] if markets else None

    async def _simulate_fill(self, side: str, bid_price: float, timeout: float = None) -> Optional[SimulatedFill]:
        """
        Simulate maker fill using real orderbook data.

        More realistic fill model:
        1. Instant fill if best_ask <= bid_price (crossed market)
        2. Probability-based fill when close to ask (simulates takers hitting our bid)
        3. Higher probability when closer to ask, lower when further

        This better models real market dynamics where someone can cross the spread
        to hit our bid even if ask hasn't dropped to our level.
        """
        if timeout is None:
            timeout = FILL_TIMEOUT

        start_time = time.time()

        while (time.time() - start_time) < timeout:
            book = self._up_book if side == "UP" else self._down_book
            best_ask = book.best_ask if book and book.best_ask is not None else 1.0
            best_bid = book.best_bid if book and book.best_bid is not None else 0.0

            # Case 1: Instant fill if ask crossed down to our bid
            if best_ask <= bid_price:
                fill_time = time.time()
                return SimulatedFill(
                    side=side,
                    price=bid_price,
                    filled_at=fill_time,
                    time_to_fill_ms=(fill_time - start_time) * 1000,
                )

            # Case 2: Probability-based fill (simulates taker hitting our bid)
            # Higher probability when:
            # - Our bid is close to best_ask (tight spread)
            # - Our bid is at or above best_bid (we're competitive)
            distance_to_ask = best_ask - bid_price

            if distance_to_ask <= 0.03:  # Only if within 3 cents of ask
                # Fill probability decreases with distance
                # At distance 0: ~8% per 100ms
                # At distance 0.01: ~4% per 100ms
                # At distance 0.02: ~2% per 100ms
                # At distance 0.03: ~1% per 100ms
                fill_prob = FILL_PROB_BASE * (1 / (distance_to_ask * 50 + 0.5))

                # Bonus if we're at or above best_bid (competitive)
                if bid_price >= best_bid:
                    fill_prob *= 1.5

                if random.random() < fill_prob:
                    fill_time = time.time()
                    return SimulatedFill(
                        side=side,
                        price=bid_price,
                        filled_at=fill_time,
                        time_to_fill_ms=(fill_time - start_time) * 1000,
                    )

            await asyncio.sleep(0.1)  # Check every 100ms

        return None  # Timeout

    async def _run_single_cycle(self, market: BTCMarket) -> Optional[CompletedCycle]:
        """
        Run a single entry → hedge cycle with velocity tracking.

        1. Wait for entry velocity reversal (should_enter_now)
        2. Place entry order, wait for fill
        3. Wait for hedge velocity reversal (should_hedge_now)
        4. Place hedge order, wait for fill
        5. Record cycle metrics
        """
        cycle_start = time.time()
        self._cycle_count += 1

        # Get market state
        up_ask, down_ask, up_bid, down_bid = self._get_orderbook_prices()

        # Determine expensive side (calc maker enters expensive first)
        expensive_side = "UP" if up_ask >= down_ask else "DOWN"
        cheap_side = "DOWN" if expensive_side == "UP" else "UP"

        logger.info(f"[CYCLE {self._cycle_count}] Starting: expensive={expensive_side}, "
                   f"UP=${up_ask:.3f}, DOWN=${down_ask:.3f}")

        # === ENTRY PHASE: Wait for velocity reversal ===
        entry_decisions = 0
        first_skipped_price: Optional[float] = None
        entry_start = time.time()

        while market.time_remaining() > 60:  # Stop if <1 min left
            velocity_bps = self._get_velocity()
            should_enter = self.strategy.should_enter_now(velocity_bps, expensive_side)

            up_ask, down_ask, up_bid, down_bid = self._get_orderbook_prices()
            current_price = up_ask if expensive_side == "UP" else down_ask

            if not should_enter:
                # Record skip decision
                entry_decisions += 1
                if first_skipped_price is None:
                    first_skipped_price = current_price

                decision = EntryDecision(
                    timestamp=time.time(),
                    market_slug=market.slug,
                    expensive_side=expensive_side,
                    velocity_bps=velocity_bps,
                    decision="SKIP_WAITING",
                    raw_up_ask=up_ask,
                    raw_down_ask=down_ask,
                    up_bid=up_bid,
                    down_bid=down_bid,
                    skipped_price=current_price,
                )
                self.entry_decisions.append(decision)

                if entry_decisions % 10 == 1:  # Log every 10th skip
                    logger.debug(f"[ENTRY] Skip #{entry_decisions}: vel={velocity_bps:.3f}bps, "
                               f"price=${current_price:.3f}")

                await asyncio.sleep(1)  # Check again in 1s
                continue

            # ENTER NOW - velocity reversal detected!
            logger.info(f"[ENTRY] Reversal detected: vel={velocity_bps:.3f}bps after {entry_decisions} skips")

            # Place entry order using dynamic threshold and sizing (matches calculus_maker.py)
            time_remaining = market.time_remaining()
            entry_threshold = self.strategy.get_threshold(time_remaining)
            entry_size = self.strategy.get_size(time_remaining)
            best_bid = up_bid if expensive_side == "UP" else down_bid
            entry_bid = best_bid - entry_threshold
            entry_bid = max(0.01, round(entry_bid, 2))

            logger.debug(f"[ENTRY] t_rem={time_remaining:.0f}s, threshold={entry_threshold:.4f}, "
                        f"size={entry_size}, bid={best_bid:.3f} -> entry_bid={entry_bid:.3f}")

            entry_fill = await self._simulate_fill(expensive_side, entry_bid, timeout=FILL_TIMEOUT)

            if not entry_fill:
                logger.warning(f"[ENTRY] Fill timeout after {FILL_TIMEOUT:.0f}s, aborting cycle")
                return None

            # Record entry decision
            decision = EntryDecision(
                timestamp=time.time(),
                market_slug=market.slug,
                expensive_side=expensive_side,
                velocity_bps=velocity_bps,
                decision="ENTER_NOW",
                raw_up_ask=up_ask,
                raw_down_ask=down_ask,
                up_bid=up_bid,
                down_bid=down_bid,
                skipped_price=first_skipped_price,
                actual_entry_price=entry_fill.price,
                time_to_fill_ms=entry_fill.time_to_fill_ms,
            )
            self.entry_decisions.append(decision)

            logger.info(f"[ENTRY] Filled @ ${entry_fill.price:.3f} in {entry_fill.time_to_fill_ms:.0f}ms")
            break
        else:
            # Market ending, couldn't enter
            return None

        # === HEDGE PHASE: Wait for velocity reversal (let it ride) ===
        entry_fill_time = time.time()
        up_ask, down_ask, up_bid, down_bid = self._get_orderbook_prices()
        immediate_hedge_price = down_ask if expensive_side == "UP" else up_ask
        hedge_decisions = 0

        # Calculate max hedge price (profit ceiling)
        min_profit = 0.005
        max_hedge_price = 1.0 - entry_fill.price - min_profit

        while market.time_remaining() > 30:  # Stop if <30s left
            time_since_fill = time.time() - entry_fill_time

            # CRITICAL: Check if unhedged for >15 minutes (spillover to next market)
            if time_since_fill > 900:  # 15 minutes = 900 seconds
                logger.error(f"[UNHEDGED >15MIN] CRITICAL: Position unhedged for {time_since_fill/60:.1f} minutes! "
                            f"market={market.slug}, entry={expensive_side}@${entry_fill.price:.3f}")
                self.unhedged_entries.append({
                    'timestamp': time.time(),
                    'market_slug': market.slug,
                    'entry_side': expensive_side,
                    'entry_price': entry_fill.price,
                    'hedge_side': cheap_side,
                    'time_unhedged_secs': time_since_fill,
                    'reason': 'spillover_15min',
                })
                return None

            velocity_bps = self._get_velocity()

            up_ask, down_ask, up_bid, down_bid = self._get_orderbook_prices()
            current_hedge_price = down_ask if expensive_side == "UP" else up_ask

            should_hedge, reason = self.strategy.should_hedge_now(
                velocity_bps=velocity_bps,
                hedge_side=cheap_side,
                time_since_entry_fill=time_since_fill,
                current_hedge_price=current_hedge_price,
                max_hedge_price=max_hedge_price,
            )

            if not should_hedge:
                # Record let-it-ride decision
                hedge_decisions += 1

                decision = HedgeDecision(
                    timestamp=time.time(),
                    market_slug=market.slug,
                    hedge_side=cheap_side,
                    velocity_bps=velocity_bps,
                    time_since_entry_fill=time_since_fill,
                    decision="LET_IT_RIDE",
                    reason=reason,
                    immediate_hedge_price=immediate_hedge_price,
                    current_hedge_price=current_hedge_price,
                )
                self.hedge_decisions.append(decision)

                if hedge_decisions % 10 == 1:  # Log every 10th ride
                    logger.debug(f"[HEDGE] Let it ride #{hedge_decisions}: vel={velocity_bps:.3f}bps, "
                               f"price=${current_hedge_price:.3f}, reason={reason}")

                await asyncio.sleep(1)
                continue

            # HEDGE NOW - velocity reversal or timeout!
            logger.info(f"[HEDGE] Trigger: {reason} after {hedge_decisions} rides, vel={velocity_bps:.3f}bps")

            # Place hedge order using dynamic threshold and sizing (matches calculus_maker.py)
            hedge_time_remaining = market.time_remaining()
            hedge_threshold = self.strategy.get_threshold(hedge_time_remaining)
            hedge_size = self.strategy.get_size(hedge_time_remaining)
            hedge_best_bid = down_bid if expensive_side == "UP" else up_bid
            hedge_bid = hedge_best_bid - hedge_threshold
            hedge_bid = max(0.01, min(hedge_bid, max_hedge_price))
            hedge_bid = round(hedge_bid, 2)

            logger.debug(f"[HEDGE] t_rem={hedge_time_remaining:.0f}s, threshold={hedge_threshold:.4f}, "
                        f"size={hedge_size}, bid={hedge_best_bid:.3f} -> hedge_bid={hedge_bid:.3f}")

            hedge_fill = await self._simulate_fill(cheap_side, hedge_bid, timeout=FILL_TIMEOUT)

            if not hedge_fill:
                # Hedge fill timeout - only ERROR if >15 minutes unhedged
                time_unhedged = time.time() - entry_fill_time
                if time_unhedged > 900:
                    logger.error(f"[UNHEDGED >15MIN] CRITICAL: Spillover! "
                                f"market={market.slug}, entry={expensive_side}@${entry_fill.price:.3f}, "
                                f"time_unhedged={time_unhedged/60:.1f}min")
                    self.unhedged_entries.append({
                        'timestamp': time.time(),
                        'market_slug': market.slug,
                        'entry_side': expensive_side,
                        'entry_price': entry_fill.price,
                        'hedge_side': cheap_side,
                        'time_unhedged_secs': time_unhedged,
                        'reason': 'spillover_15min',
                    })
                else:
                    logger.warning(f"[HEDGE] Fill timeout after {FILL_TIMEOUT:.0f}s (unhedged {time_unhedged:.0f}s)")
                return None

            # Record hedge decision
            decision = HedgeDecision(
                timestamp=time.time(),
                market_slug=market.slug,
                hedge_side=cheap_side,
                velocity_bps=velocity_bps,
                time_since_entry_fill=time_since_fill,
                decision="HEDGE_NOW",
                reason=reason,
                immediate_hedge_price=immediate_hedge_price,
                current_hedge_price=current_hedge_price,
                actual_hedge_price=hedge_fill.price,
            )
            self.hedge_decisions.append(decision)

            logger.info(f"[HEDGE] Filled @ ${hedge_fill.price:.3f} in {hedge_fill.time_to_fill_ms:.0f}ms")
            break
        else:
            # Market ending before hedge - only ERROR if >15 minutes unhedged
            time_unhedged = time.time() - entry_fill_time
            if time_unhedged > 900:
                logger.error(f"[UNHEDGED >15MIN] CRITICAL: Spillover! "
                            f"market={market.slug}, entry={expensive_side}@${entry_fill.price:.3f}, "
                            f"time_unhedged={time_unhedged/60:.1f}min")
                self.unhedged_entries.append({
                    'timestamp': time.time(),
                    'market_slug': market.slug,
                    'entry_side': expensive_side,
                    'entry_price': entry_fill.price,
                    'hedge_side': cheap_side,
                    'time_unhedged_secs': time_unhedged,
                    'reason': 'spillover_15min',
                })
            else:
                logger.warning(f"[HEDGE] Market ending (unhedged {time_unhedged:.0f}s)")
            return None

        # === RECORD COMPLETED CYCLE ===
        pair_cost = entry_fill.price + hedge_fill.price
        profit = 1.0 - pair_cost

        # Use min(entry_size, hedge_size) for actual position size
        # (in reality both sides should match for a complete pair)
        position_size = min(entry_size, hedge_size)
        profit_dollars = profit * position_size

        # Calculate improvements in basis points
        baseline_entry = first_skipped_price if first_skipped_price else entry_fill.price
        entry_improvement_bps = (baseline_entry - entry_fill.price) * 10000
        hedge_improvement_bps = (immediate_hedge_price - hedge_fill.price) * 10000

        cycle = CompletedCycle(
            cycle_id=self._cycle_count,
            market_slug=market.slug,
            timestamp=time.time(),
            entry_side=expensive_side,
            entry_price=entry_fill.price,
            entry_size=entry_size,
            entry_decisions_count=entry_decisions,
            entry_velocity_at_fill=velocity_bps,
            first_skipped_price=baseline_entry,
            entry_improvement_bps=entry_improvement_bps,
            time_to_entry_fill_ms=entry_fill.time_to_fill_ms,
            entry_threshold=entry_threshold,
            hedge_side=cheap_side,
            hedge_price=hedge_fill.price,
            hedge_size=hedge_size,
            hedge_decisions_count=hedge_decisions,
            hedge_velocity_at_fill=velocity_bps,
            hedge_reason="reversal" if "reversal" in reason.lower() else "force_timeout",
            immediate_hedge_price=immediate_hedge_price,
            hedge_improvement_bps=hedge_improvement_bps,
            time_to_hedge_fill_ms=hedge_fill.time_to_fill_ms,
            hedge_threshold=hedge_threshold,
            pair_cost=pair_cost,
            profit=profit,
            profit_dollars=profit_dollars,
            total_time_ms=(time.time() - cycle_start) * 1000,
        )
        self.completed_cycles.append(cycle)

        # Save immediately (incremental) to prevent data loss on disconnect
        self._save_cycle_incremental(cycle)

        logger.info(f"[CYCLE {self._cycle_count}] Complete: size={position_size}, pair=${pair_cost:.3f}, "
                   f"profit=${profit:.3f} (${profit_dollars:.2f}), entry_improve={entry_improvement_bps:.1f}bps, "
                   f"hedge_improve={hedge_improvement_bps:.1f}bps")

        return cycle

    async def _rotate_market_if_needed(self) -> bool:
        """Check if market expired and rotate to new one."""
        if not self.current_market:
            return False

        time_left = self.current_market.time_remaining()
        if time_left > 30:
            return False

        logger.info(f"[ROTATION] Market expiring: {self.current_market.slug} - {time_left:.0f}s left")

        # Find new market
        new_market = await self.find_active_market()
        if not new_market or new_market.slug == self.current_market.slug:
            logger.info("[ROTATION] No new market yet, waiting...")
            return False

        # Rotate
        self.current_market = new_market
        self.strike_price = self.binance_client.current_price
        self.binance_client.set_strike_price(self.strike_price)

        # Clear orderbook
        self._up_book = None
        self._down_book = None

        # Reconnect WebSocket
        logger.info("[ROTATION] Reconnecting WebSocket...")

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        await self.ws_client.disconnect()
        if not await self.ws_client.connect():
            logger.error("[ROTATION] Failed to reconnect WebSocket!")
            return False

        token_ids = [self.current_market.up_token_id, self.current_market.down_token_id]
        if not await self.ws_client.subscribe(token_ids):
            logger.error("[ROTATION] Failed to subscribe!")
            return False

        self._ws_task = asyncio.create_task(self.ws_client.run())

        # Wait for orderbook
        for _ in range(50):
            if self._up_book and self._down_book:
                logger.info(f"[ROTATION] New market: {new_market.slug}")
                break
            await asyncio.sleep(0.1)

        return True

    async def run(self, hours: float = 8.0):
        """Run simulation for specified hours."""
        end_time = time.time() + (hours * 3600)

        logger.info(f"\n{'#'*60}")
        logger.info(f"# CALC MAKER VELOCITY SIMULATOR")
        logger.info(f"# Duration: {hours} hours")
        logger.info(f"# Output: {self.output_dir}")
        logger.info(f"{'#'*60}\n")

        await self.initialize()

        # Find active market
        self.current_market = await self.find_active_market()
        if not self.current_market:
            logger.error("No active market found!")
            return

        self.strike_price = self.binance_client.current_price
        self.binance_client.set_strike_price(self.strike_price)

        logger.info(f"Market: {self.current_market.slug}")
        logger.info(f"Strike: ${self.strike_price:,.2f}")
        logger.info(f"Time remaining: {self.current_market.time_remaining():.0f}s")

        # Connect WebSocket
        if not await self.ws_client.connect():
            logger.error("Failed to connect WebSocket!")
            return

        token_ids = [self.current_market.up_token_id, self.current_market.down_token_id]
        if not await self.ws_client.subscribe(token_ids):
            logger.error("Failed to subscribe!")
            return

        self._ws_task = asyncio.create_task(self.ws_client.run())

        # Wait for orderbook
        for _ in range(50):
            if self._up_book and self._down_book:
                break
            await asyncio.sleep(0.1)

        if not self._up_book or not self._down_book:
            logger.warning("No orderbook data received!")

        # Main loop
        last_market_check = 0.0

        try:
            while time.time() < end_time:
                now = time.time()

                # Record velocity sample (every 1s) for threshold analysis
                self._record_velocity_sample()

                # Check market rotation every 10s
                if now - last_market_check > 10.0:
                    await self._rotate_market_if_needed()
                    last_market_check = now

                # Wait for gate duration at market open
                time_remaining = self.current_market.time_remaining() if self.current_market else 0
                time_elapsed = 900 - time_remaining

                if time_elapsed < GATE_DURATION:
                    logger.debug(f"[GATE] Waiting {GATE_DURATION - time_elapsed:.0f}s for market to stabilize")
                    await asyncio.sleep(1)
                    continue

                # Skip if <60s remaining
                if time_remaining < 60:
                    await asyncio.sleep(1)
                    continue

                # Run a cycle
                if self._up_book and self._down_book:
                    await self._run_single_cycle(self.current_market)

                # Brief pause between cycles
                await asyncio.sleep(2)

        except KeyboardInterrupt:
            logger.info("\nSimulation interrupted by user")
        finally:
            # Cleanup
            if self._ws_task:
                self._ws_task.cancel()
            if self.ws_client:
                await self.ws_client.disconnect()
            if self.binance_client:
                await self.binance_client.disconnect()

        # Write results
        self._write_results()

    def _write_results(self):
        """Write CSV and print summary statistics."""
        # Use the same timestamp from run start for consistency
        timestamp = self._run_timestamp

        # Cycles CSV already saved incrementally - just get the path
        cycles_path = self._cycles_csv_path or self.output_dir / f"calc_velocity_sim_{timestamp}.csv"

        # If no cycles were saved incrementally, write them now (fallback)
        if self._cycles_csv_path is None and self.completed_cycles:
            with open(cycles_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'cycle_id', 'market_slug', 'timestamp',
                    'entry_side', 'entry_price', 'entry_size', 'entry_threshold',
                    'entry_decisions', 'entry_improvement_bps',
                    'first_skipped_price', 'time_to_entry_fill_ms',
                    'hedge_side', 'hedge_price', 'hedge_size', 'hedge_threshold',
                    'hedge_decisions', 'hedge_improvement_bps',
                    'hedge_reason', 'immediate_hedge_price', 'time_to_hedge_fill_ms',
                    'pair_cost', 'profit', 'profit_dollars', 'total_time_ms'
                ])
                for c in self.completed_cycles:
                    writer.writerow([
                        c.cycle_id, c.market_slug, c.timestamp,
                        c.entry_side, c.entry_price, c.entry_size, c.entry_threshold,
                        c.entry_decisions_count, c.entry_improvement_bps,
                        c.first_skipped_price, c.time_to_entry_fill_ms,
                        c.hedge_side, c.hedge_price, c.hedge_size, c.hedge_threshold,
                        c.hedge_decisions_count, c.hedge_improvement_bps,
                        c.hedge_reason, c.immediate_hedge_price, c.time_to_hedge_fill_ms,
                        c.pair_cost, c.profit, c.profit_dollars, c.total_time_ms
                    ])

        # Write decisions CSV (detailed)
        decisions_path = self.output_dir / f"calc_velocity_decisions_{timestamp}.csv"
        with open(decisions_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'type', 'market_slug', 'side', 'velocity_bps',
                'decision', 'reason', 'skipped_price', 'actual_price', 'improvement_bps'
            ])
            for d in self.entry_decisions:
                improvement = ((d.skipped_price or 0) - (d.actual_entry_price or 0)) * 10000 if d.actual_entry_price else 0
                writer.writerow([
                    d.timestamp, 'ENTRY', d.market_slug, d.expensive_side, d.velocity_bps,
                    d.decision, '', d.skipped_price, d.actual_entry_price, improvement
                ])
            for d in self.hedge_decisions:
                improvement = (d.immediate_hedge_price - (d.actual_hedge_price or d.current_hedge_price)) * 10000
                writer.writerow([
                    d.timestamp, 'HEDGE', d.market_slug, d.hedge_side, d.velocity_bps,
                    d.decision, d.reason, d.immediate_hedge_price, d.actual_hedge_price, improvement
                ])

        # Write velocity time series CSV
        velocity_path = self.output_dir / f"calc_velocity_timeseries_{timestamp}.csv"
        with open(velocity_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'market_slug', 'btc_price', 'velocity_bps',
                'up_ask', 'down_ask', 'up_bid', 'down_bid', 'expensive_side'
            ])
            for s in self.velocity_samples:
                writer.writerow([
                    s.timestamp, s.market_slug, s.btc_price, s.velocity_bps,
                    s.up_ask, s.down_ask, s.up_bid, s.down_bid, s.expensive_side
                ])

        # Print summary
        print(f"\n{'='*60}")
        print(f"CALC MAKER VELOCITY SIMULATION RESULTS")
        print(f"{'='*60}")

        if not self.completed_cycles:
            print("No completed cycles.")
            return

        n = len(self.completed_cycles)
        avg_pair_cost = sum(c.pair_cost for c in self.completed_cycles) / n
        avg_profit = sum(c.profit for c in self.completed_cycles) / n
        avg_entry_improvement = sum(c.entry_improvement_bps for c in self.completed_cycles) / n
        avg_hedge_improvement = sum(c.hedge_improvement_bps for c in self.completed_cycles) / n
        avg_entry_decisions = sum(c.entry_decisions_count for c in self.completed_cycles) / n
        avg_hedge_decisions = sum(c.hedge_decisions_count for c in self.completed_cycles) / n

        reversal_count = sum(1 for c in self.completed_cycles if c.hedge_reason == "reversal")
        timeout_count = n - reversal_count

        profitable_count = sum(1 for c in self.completed_cycles if c.profit > 0)

        # Position sizing stats
        avg_entry_size = sum(c.entry_size for c in self.completed_cycles) / n
        avg_hedge_size = sum(c.hedge_size for c in self.completed_cycles) / n
        total_profit_dollars = sum(c.profit_dollars for c in self.completed_cycles)
        avg_profit_dollars = total_profit_dollars / n

        # Threshold stats
        avg_entry_threshold = sum(c.entry_threshold for c in self.completed_cycles) / n
        avg_hedge_threshold = sum(c.hedge_threshold for c in self.completed_cycles) / n

        print(f"Total cycles: {n}")
        print(f"Profitable: {profitable_count}/{n} ({100*profitable_count/n:.1f}%)")
        print(f"")
        print(f"PROFIT SUMMARY:")
        print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
        print(f"  Avg profit/share: ${avg_profit:.4f}")
        print(f"  Total profit/share: ${sum(c.profit for c in self.completed_cycles):.4f}")
        print(f"  Avg profit/cycle: ${avg_profit_dollars:.2f}")
        print(f"  Total profit ($): ${total_profit_dollars:.2f}")
        print(f"")
        print(f"POSITION SIZING (dynamic, matches calculus_maker.py):")
        print(f"  Avg entry size: {avg_entry_size:.1f} shares")
        print(f"  Avg hedge size: {avg_hedge_size:.1f} shares")
        print(f"  Avg entry threshold: {avg_entry_threshold:.4f}")
        print(f"  Avg hedge threshold: {avg_hedge_threshold:.4f}")
        print(f"")
        print(f"VELOCITY TIMING EFFECTIVENESS:")
        print(f"  Entry improvement: {avg_entry_improvement:.2f} bps avg")
        print(f"  Hedge improvement: {avg_hedge_improvement:.2f} bps avg")
        print(f"  Total improvement: {avg_entry_improvement + avg_hedge_improvement:.2f} bps avg")
        print(f"")
        print(f"DECISION COUNTS:")
        print(f"  Avg entry SKIPs before ENTER: {avg_entry_decisions:.1f}")
        print(f"  Avg hedge LET_RIDEs before HEDGE: {avg_hedge_decisions:.1f}")
        print(f"")
        print(f"HEDGE TRIGGERS:")
        print(f"  Velocity reversals: {reversal_count}/{n} ({100*reversal_count/n:.1f}%)")
        print(f"  Force timeouts: {timeout_count}/{n} ({100*timeout_count/n:.1f}%)")
        print(f"")

        # Velocity threshold analysis
        if self.velocity_samples:
            print(f"VELOCITY THRESHOLD ANALYSIS:")
            print(f"  Total samples: {len(self.velocity_samples)}")

            velocities = [abs(s.velocity_bps) for s in self.velocity_samples]
            avg_abs_velocity = sum(velocities) / len(velocities)
            max_velocity = max(velocities)
            min_velocity = min(velocities)

            print(f"  Avg |velocity|: {avg_abs_velocity:.3f} bps")
            print(f"  Max |velocity|: {max_velocity:.3f} bps")
            print(f"  Min |velocity|: {min_velocity:.3f} bps")
            print(f"")

            # Analyze how often velocity exceeds different thresholds
            thresholds = [0.025, 0.05, 0.075, 0.1, 0.2, 0.5]
            print(f"  Threshold analysis (times velocity exceeded threshold):")
            for thresh in thresholds:
                exceeds = sum(1 for v in velocities if v >= thresh)
                pct = 100 * exceeds / len(velocities)
                current_marker = " <- CURRENT" if thresh == 0.05 else ""
                print(f"    {thresh:.3f} bps: {exceeds}/{len(velocities)} ({pct:.1f}%){current_marker}")

            print(f"")
            print(f"  Velocity time series: {velocity_path}")
            print(f"")

        # Write unhedged errors CSV if any
        if self.unhedged_entries:
            errors_path = self.output_dir / f"calc_velocity_ERRORS_{timestamp}.csv"
            with open(errors_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_slug', 'entry_side', 'entry_price',
                    'hedge_side', 'time_unhedged_secs', 'reason'
                ])
                for e in self.unhedged_entries:
                    writer.writerow([
                        e['timestamp'], e['market_slug'], e['entry_side'], e['entry_price'],
                        e['hedge_side'], e['time_unhedged_secs'], e['reason']
                    ])

            print(f"ERRORS - UNHEDGED POSITIONS:")
            print(f"  Count: {len(self.unhedged_entries)}")
            for e in self.unhedged_entries:
                print(f"    - {e['market_slug']}: {e['entry_side']}@${e['entry_price']:.3f}, "
                      f"unhedged {e['time_unhedged_secs']:.0f}s, reason={e['reason']}")
            print(f"  Error file: {errors_path}")
            print(f"")

        print(f"OUTPUT FILES:")
        print(f"  Cycles: {cycles_path}")
        print(f"  Decisions: {decisions_path}")
        print(f"  Velocity Time Series: {velocity_path}")
        if self.unhedged_entries:
            print(f"  ERRORS: {errors_path}")
        print(f"{'='*60}\n")


# =============================================================================
# MAIN
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description='Calc Maker Velocity Effectiveness Simulator')
    parser.add_argument('--hours', type=float, default=8.0, help='Duration in hours (default: 8)')
    parser.add_argument('--output', type=str, default='research/', help='Output directory')
    args = parser.parse_args()

    sim = CalcMakerVelocitySim(output_dir=args.output)
    await sim.run(hours=args.hours)


if __name__ == "__main__":
    asyncio.run(main())
