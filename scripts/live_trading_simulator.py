#!/usr/bin/env python3
"""
Live Trading Simulator - Speed Edge Analysis

Tests if speed provides a trading edge using real-time WebSocket feeds.
Records hypothetical trading activity WITHOUT placing real orders.

Architecture:
- Market Detection: REST API (MarketFinder) - finds active BTC 15-min markets
- Orderbook Data: WebSocket (WebSocketClient) - real-time book updates for speed
- Price Feed: WebSocket (BinanceClient) - real-time BTC price for velocity calculation

Two simulation modes:
1. Two-Sided Orders: Place bids on both sides, cancel losing side on velocity trigger
2. Expensive Side First: Place bid on expensive side, velocity pulling (cancel+reprice)

Usage:
    python scripts/live_trading_simulator.py --mode two_sided --latency 300 --duration 15
    python scripts/live_trading_simulator.py --mode expensive_first --duration 15
"""

import argparse
import asyncio
import csv
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder
from src.services.trend_detector import TrendDetector
from src.models.market import BTCMarket


class SimulationMode(Enum):
    TWO_SIDED = "two_sided"
    EXPENSIVE_FIRST = "expensive_first"


class OrderStatus(Enum):
    PENDING = "pending"       # Order placed, waiting for fill
    FILLED = "filled"         # Order filled
    CANCELLED = "cancelled"   # Order cancelled
    FAILED = "failed"         # Cancel failed - filled during latency window


@dataclass
class SimulatedOrder:
    """A simulated order in the simulator."""
    order_id: str
    side: str               # "UP" or "DOWN"
    price: float            # Bid price
    size: int
    placed_at: float        # Timestamp when placed
    status: OrderStatus = OrderStatus.PENDING
    filled_at: Optional[float] = None
    fill_price: Optional[float] = None
    cancelled_at: Optional[float] = None
    cancel_reason: Optional[str] = None


@dataclass
class TradeCycle:
    """Records one complete entry+hedge cycle."""
    cycle_id: int
    mode: str
    latency_ms: int
    market_slug: str = ""  # Which market this cycle traded on

    # Entry details
    entry_side: str = ""
    entry_bid_price: float = 0.0
    entry_placed_at: float = 0.0
    entry_filled_at: float = 0.0
    entry_fill_price: float = 0.0
    time_to_entry_fill_ms: float = 0.0

    # Hedge details
    hedge_side: str = ""
    hedge_bid_price: float = 0.0
    hedge_placed_at: float = 0.0
    hedge_filled_at: float = 0.0
    hedge_fill_price: float = 0.0
    time_to_hedge_fill_ms: float = 0.0

    # Results
    pair_cost: float = 0.0
    profit: float = 0.0
    spread_captured: float = 0.0
    total_time_ms: float = 0.0

    # Conditions at entry
    velocity_at_entry: float = 0.0
    btc_price_at_entry: float = 0.0
    up_ask_at_entry: float = 0.0
    down_ask_at_entry: float = 0.0
    up_bid_at_entry: float = 0.0
    down_bid_at_entry: float = 0.0

    # Two-sided specific
    cancelled_side: str = ""
    adverse_fill: bool = False  # Did losing side fill before cancel completed?
    velocity_triggered: bool = False  # True if cancellation was due to velocity (spread capture)
    velocity_at_cancel: float = 0.0   # Velocity when cancel triggered
    time_to_velocity_trigger_ms: float = 0.0  # Time from entry to velocity trigger

    # Expensive-first velocity pulling
    entry_pull_count: int = 0  # Number of times entry was pulled due to velocity
    entry_last_pull_velocity: float = 0.0  # Velocity at last pull


class LiveTradingSimulator:
    """
    Live trading simulator using real WebSocket feeds.

    Records hypothetical trading activity without placing real orders.
    """

    # Velocity threshold for cancel trigger (basis points per second)
    # $5 BTC move in 10sec = 0.055% = 5.5bps total = 0.055 bps/sec
    # Set to 0.05 to catch moderate moves
    VELOCITY_THRESHOLD_BPS = 0.05

    # Target spread for profitable hedge
    TARGET_SPREAD = 0.02

    # Two-sided spread capture parameters
    TWO_SIDED_ENTRY_OFFSET = 0.00      # Place at best_bid (was 0.03 - too slow)
    TWO_SIDED_MAX_WAIT_SECS = 10.0     # Max time to wait for velocity signal
    TWO_SIDED_MIN_VELOCITY = 0.05      # Match VELOCITY_THRESHOLD_BPS

    # Expensive-first velocity pulling parameters
    MAX_ENTRY_PULLS = 3                # Max times to pull entry before giving up
    ENTRY_TIMEOUT_SECS = 10.0          # Reprice entry if not filled after this time

    def __init__(
        self,
        simulation_mode: SimulationMode,
        latency_ms: int = 300,
        output_dir: Path = Path("research"),
    ):
        self.simulation_mode = simulation_mode
        self.latency_ms = latency_ms
        self.output_dir = output_dir

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

        # Simulation state
        self._pending_orders: Dict[str, SimulatedOrder] = {}
        self._completed_cycles: List[TradeCycle] = []
        self._current_cycle: Optional[TradeCycle] = None
        self._cycle_count: int = 0
        self._in_trade: bool = False

        # Event log for detailed analysis
        self._events: List[Dict[str, Any]] = []

    def _on_book_update(self, update: BookUpdate):
        """Callback for WebSocket book updates."""
        if not self.current_market:
            return

        if update.token_id == self.current_market.up_token_id:
            self._up_book = update
        elif update.token_id == self.current_market.down_token_id:
            self._down_book = update

        # Check for simulated fills on every book update
        self._check_fills()

    def _check_fills(self):
        """Check if any pending orders would fill based on current orderbook."""
        now = time.time()

        # TWO-SIDED: Check velocity BEFORE processing fills (fixes race condition)
        # The main loop's velocity check never fires because fills happen in this callback
        if (self.simulation_mode == SimulationMode.TWO_SIDED
            and self._current_cycle
            and not self._current_cycle.entry_filled_at
            and not self._current_cycle.cancelled_side):

            # Check if velocity should trigger cancellation
            should_cancel, losing_side = self._should_trigger_cancel()
            if should_cancel:
                order = self._find_order_by_side(losing_side)
                if order and order.status == OrderStatus.PENDING:
                    cycle = self._current_cycle
                    signal = self.trend_detector.get_trend_signal() if self.trend_detector else None
                    velocity = signal.velocity_bps if signal else 0.0
                    elapsed = now - cycle.entry_placed_at

                    cycle.velocity_triggered = True
                    cycle.velocity_at_cancel = velocity
                    cycle.time_to_velocity_trigger_ms = elapsed * 1000
                    cycle.cancelled_side = losing_side

                    self._cancel_order(order, f"Velocity trigger: {losing_side} losing (vel={velocity:.1f}bps)")

                    self._log_event("VELOCITY_CANCEL_IN_FILL_CHECK", {
                        "cancelled_side": losing_side,
                        "velocity_bps": velocity,
                        "elapsed_ms": elapsed * 1000,
                        "kept_side": "DOWN" if losing_side == "UP" else "UP",
                    })

        # EXPENSIVE-FIRST: Velocity pulling - cancel and reprice on SAME side
        # This avoids fills during high volatility (quote pulling)
        if (self.simulation_mode == SimulationMode.EXPENSIVE_FIRST
            and self._current_cycle
            and not self._current_cycle.entry_filled_at
            and self._current_cycle.entry_pull_count < self.MAX_ENTRY_PULLS):

            should_cancel, _ = self._should_trigger_cancel()
            if should_cancel:
                cycle = self._current_cycle
                signal = self.trend_detector.get_trend_signal() if self.trend_detector else None
                velocity = signal.velocity_bps if signal else 0.0

                # Find the entry order and cancel it
                entry_order = self._find_order_by_side(cycle.entry_side)
                if entry_order and entry_order.status == OrderStatus.PENDING:
                    # Cancel the entry
                    self._cancel_order(entry_order, f"Velocity pull: reprice (vel={velocity:.2f}bps)")

                    # Update cycle tracking
                    cycle.entry_pull_count += 1
                    cycle.entry_last_pull_velocity = velocity

                    # Get new best_bid and reprice
                    if cycle.entry_side == "UP" and self._up_book:
                        new_price = self._up_book.best_bid or 0.01
                    elif cycle.entry_side == "DOWN" and self._down_book:
                        new_price = self._down_book.best_bid or 0.01
                    else:
                        new_price = cycle.entry_bid_price  # Fallback to same price

                    new_price = max(0.01, round(new_price, 2))

                    # Create new order at new price
                    new_order = self._create_order(cycle.entry_side, new_price, 5)
                    cycle.entry_bid_price = new_price
                    cycle.entry_placed_at = now

                    self._log_event("VELOCITY_PULL_REPRICE", {
                        "side": cycle.entry_side,
                        "old_price": entry_order.price,
                        "new_price": new_price,
                        "velocity_bps": velocity,
                        "pull_count": cycle.entry_pull_count,
                    })

        # EXPENSIVE-FIRST: Timeout reprice - reprice stale orders
        # If order hasn't filled after ENTRY_TIMEOUT_SECS, reprice at current best_bid
        if (self.simulation_mode == SimulationMode.EXPENSIVE_FIRST
            and self._current_cycle
            and not self._current_cycle.entry_filled_at):

            cycle = self._current_cycle
            entry_order = self._find_order_by_side(cycle.entry_side)

            if entry_order and entry_order.status == OrderStatus.PENDING:
                elapsed = now - entry_order.placed_at

                if elapsed > self.ENTRY_TIMEOUT_SECS:
                    # Get current best_bid
                    if cycle.entry_side == "UP" and self._up_book:
                        current_bid = self._up_book.best_bid or 0.01
                    elif cycle.entry_side == "DOWN" and self._down_book:
                        current_bid = self._down_book.best_bid or 0.01
                    else:
                        current_bid = entry_order.price

                    current_bid = max(0.01, round(current_bid, 2))

                    # Only reprice if price has changed
                    if abs(current_bid - entry_order.price) > 0.005:
                        self._cancel_order(entry_order, f"Timeout reprice: {entry_order.price:.2f} -> {current_bid:.2f}")

                        new_order = self._create_order(cycle.entry_side, current_bid, 5)
                        cycle.entry_bid_price = current_bid
                        cycle.entry_placed_at = now

                        self._log_event("TIMEOUT_REPRICE", {
                            "side": cycle.entry_side,
                            "old_price": entry_order.price,
                            "new_price": current_bid,
                            "elapsed_secs": elapsed,
                        })

        for order_id, order in list(self._pending_orders.items()):
            if order.status != OrderStatus.PENDING:
                continue

            # Get current best ask for this side
            if order.side == "UP" and self._up_book:
                best_ask = self._up_book.best_ask or 1.0
            elif order.side == "DOWN" and self._down_book:
                best_ask = self._down_book.best_ask or 1.0
            else:
                continue

            # Fill if best_ask <= our bid price (maker fill)
            if best_ask <= order.price:
                order.status = OrderStatus.FILLED
                order.filled_at = now
                order.fill_price = order.price  # Maker fill at our price

                self._log_event("ORDER_FILLED", {
                    "order_id": order_id,
                    "side": order.side,
                    "bid_price": order.price,
                    "fill_price": order.fill_price,
                    "best_ask": best_ask,
                    "time_to_fill_ms": (now - order.placed_at) * 1000,
                })

                # Process the fill
                self._on_order_filled(order)

    def _on_order_filled(self, order: SimulatedOrder):
        """Handle a simulated order fill."""
        if not self._current_cycle:
            return

        cycle = self._current_cycle
        now = time.time()

        if self.simulation_mode == SimulationMode.TWO_SIDED:
            self._handle_two_sided_fill(order, cycle, now)
        else:
            self._handle_expensive_first_fill(order, cycle, now)

    def _handle_two_sided_fill(self, order: SimulatedOrder, cycle: TradeCycle, now: float):
        """
        Handle fill in two-sided mode.

        Two scenarios:
        1. VELOCITY TRIGGERED: cancelled_side already set, this fill is our entry on kept side
        2. FILL FIRST: No cancel yet, this fill triggers cancel of other side

        In both cases, after entry fill we place hedge on the opposite side.
        """
        if not cycle.entry_filled_at:
            # First fill is our entry
            cycle.entry_side = order.side
            cycle.entry_bid_price = order.price  # Record actual bid for filled side
            cycle.entry_filled_at = now
            cycle.entry_fill_price = order.fill_price
            cycle.time_to_entry_fill_ms = (now - cycle.entry_placed_at) * 1000

            other_side = "DOWN" if order.side == "UP" else "UP"

            # Check if velocity already cancelled the other side
            if cycle.cancelled_side:
                # VELOCITY TRIGGERED PATH - other side already cancelled
                # This is the spread capture scenario - we got a cheaper fill
                self._log_event("VELOCITY_ENTRY_FILL", {
                    "side": order.side,
                    "fill_price": order.fill_price,
                    "velocity_triggered": cycle.velocity_triggered,
                    "velocity_at_cancel": cycle.velocity_at_cancel,
                })
            else:
                # FILL FIRST PATH - need to cancel other side now
                other_order = self._find_order_by_side(other_side)
                if other_order and other_order.status == OrderStatus.PENDING:
                    cycle.cancelled_side = other_side
                    cycle.velocity_triggered = False  # Fill happened before velocity
                    self._cancel_order(other_order, "Entry filled on opposite side")

            # Place hedge on opposite side
            cycle.hedge_side = other_side
            self._place_hedge_order(cycle)

        else:
            # Second fill is our hedge
            if order.side == cycle.hedge_side:
                cycle.hedge_filled_at = now
                cycle.hedge_fill_price = order.fill_price
                cycle.time_to_hedge_fill_ms = (now - cycle.hedge_placed_at) * 1000
                self._complete_cycle(cycle)

    def _handle_expensive_first_fill(self, order: SimulatedOrder, cycle: TradeCycle, now: float):
        """Handle fill in expensive-first mode."""
        if not cycle.entry_filled_at:
            # Entry filled
            cycle.entry_side = order.side
            cycle.entry_filled_at = now
            cycle.entry_fill_price = order.fill_price
            cycle.time_to_entry_fill_ms = (now - cycle.entry_placed_at) * 1000

            # Place hedge on opposite side
            cycle.hedge_side = "DOWN" if order.side == "UP" else "UP"
            self._place_hedge_order(cycle)
        else:
            # Hedge filled
            if order.side == cycle.hedge_side:
                cycle.hedge_filled_at = now
                cycle.hedge_fill_price = order.fill_price
                cycle.time_to_hedge_fill_ms = (now - cycle.hedge_placed_at) * 1000
                self._complete_cycle(cycle)

    def _place_hedge_order(self, cycle: TradeCycle):
        """Place a hedge order to complete the pair."""
        # Calculate hedge price for target spread
        max_hedge_price = (1.0 - self.TARGET_SPREAD) - cycle.entry_fill_price

        # Get current best bid for hedge side
        if cycle.hedge_side == "UP" and self._up_book:
            best_bid = self._up_book.best_bid or 0.0
        elif cycle.hedge_side == "DOWN" and self._down_book:
            best_bid = self._down_book.best_bid or 0.0
        else:
            best_bid = 0.40  # Fallback

        # Place at best_bid, but cap at max_hedge_price
        hedge_price = min(best_bid, max_hedge_price)
        hedge_price = max(0.01, round(hedge_price, 2))

        order = self._create_order(cycle.hedge_side, hedge_price, 5)
        cycle.hedge_bid_price = hedge_price
        cycle.hedge_placed_at = time.time()

        self._log_event("HEDGE_PLACED", {
            "side": cycle.hedge_side,
            "price": hedge_price,
            "max_price": max_hedge_price,
            "entry_fill": cycle.entry_fill_price,
        })

    def _cancel_order(self, order: SimulatedOrder, reason: str):
        """Simulate cancelling an order (with latency)."""
        # During the latency window, the order might still fill
        # This simulates the race condition
        cancel_time = time.time()

        # Check if order filled during our cancel latency
        # (This is a simplification - in reality we'd need to track this over time)
        if order.status == OrderStatus.FILLED:
            # Adverse fill - order filled before our cancel reached the exchange
            order.cancel_reason = f"ADVERSE FILL - {reason}"
            if self._current_cycle:
                self._current_cycle.adverse_fill = True
            self._log_event("ADVERSE_FILL", {
                "order_id": order.order_id,
                "side": order.side,
                "reason": reason,
            })
        else:
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = cancel_time
            order.cancel_reason = reason

            self._log_event("ORDER_CANCELLED", {
                "order_id": order.order_id,
                "side": order.side,
                "reason": reason,
            })

    def _complete_cycle(self, cycle: TradeCycle):
        """Complete a trade cycle and record results."""
        cycle.pair_cost = cycle.entry_fill_price + cycle.hedge_fill_price
        cycle.profit = 1.0 - cycle.pair_cost
        cycle.spread_captured = cycle.up_ask_at_entry + cycle.down_ask_at_entry - cycle.pair_cost
        cycle.total_time_ms = (cycle.hedge_filled_at - cycle.entry_placed_at) * 1000

        self._completed_cycles.append(cycle)
        self._current_cycle = None
        self._in_trade = False

        # Clear pending orders
        self._pending_orders.clear()

        self._log_event("CYCLE_COMPLETE", {
            "cycle_id": cycle.cycle_id,
            "pair_cost": cycle.pair_cost,
            "profit": cycle.profit,
            "total_time_ms": cycle.total_time_ms,
            "adverse_fill": cycle.adverse_fill,
        })

        # Print progress
        market_slug = self.current_market.slug if self.current_market else "unknown"
        vel_tag = "VEL" if cycle.velocity_triggered else "FILL"
        print(
            f"[{market_slug}] Cycle {cycle.cycle_id}: "
            f"pair=${cycle.pair_cost:.4f} "
            f"profit=${cycle.profit:.4f} "
            f"time={cycle.total_time_ms:.0f}ms "
            f"[{vel_tag}] "
            f"{'ADVERSE' if cycle.adverse_fill else 'OK'}"
        )

    def _create_order(self, side: str, price: float, size: int) -> SimulatedOrder:
        """Create and track a simulated order."""
        order_id = f"{side}_{time.time():.3f}"
        order = SimulatedOrder(
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            placed_at=time.time(),
        )
        self._pending_orders[order_id] = order
        return order

    def _find_order_by_side(self, side: str) -> Optional[SimulatedOrder]:
        """Find a pending order by side."""
        for order in self._pending_orders.values():
            if order.side == side and order.status == OrderStatus.PENDING:
                return order
        return None

    def _log_event(self, event_type: str, data: Dict[str, Any]):
        """Log an event for later analysis."""
        event = {
            "timestamp": time.time(),
            "event_type": event_type,
            **data,
        }
        self._events.append(event)

    def _should_trigger_cancel(self) -> tuple[bool, str]:
        """Check if velocity trigger should cancel an order."""
        if not self.trend_detector:
            return False, ""

        signal = self.trend_detector.get_trend_signal()
        if not signal:
            return False, ""

        velocity = signal.velocity_bps
        if abs(velocity) > self.VELOCITY_THRESHOLD_BPS:
            # Velocity exceeds threshold - determine losing side
            if velocity > 0:
                # BTC rising - DOWN is losing
                return True, "DOWN"
            else:
                # BTC falling - UP is losing
                return True, "UP"

        return False, ""

    async def initialize(self):
        """Initialize all clients."""
        print("Initializing simulator...")

        # Binance client
        self.binance_client = BinanceClient()
        await self.binance_client.connect()

        # Wait for Binance connection
        print("Waiting for Binance WebSocket...")
        for i in range(100):
            if self.binance_client.is_connected and self.binance_client.current_price > 0:
                break
            await asyncio.sleep(0.1)

        if not self.binance_client.is_connected or self.binance_client.current_price <= 0:
            raise RuntimeError("Failed to connect to Binance WebSocket")

        print(f"Binance connected! BTC: ${self.binance_client.current_price:,.2f}")

        # Trend detector
        self.trend_detector = TrendDetector(self.binance_client)

        # Market finder
        self.market_finder = MarketFinder()

        # Polymarket WebSocket
        self.ws_client = WebSocketClient(auto_reconnect=True)
        self.ws_client.on_book_update(self._on_book_update)

        print("Simulator initialized.")

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

    def _start_new_cycle(self):
        """Start a new trade cycle."""
        if self._in_trade or not self._up_book or not self._down_book:
            return

        self._cycle_count += 1
        self._in_trade = True

        now = time.time()
        signal = self.trend_detector.get_trend_signal() if self.trend_detector else None

        # Record market conditions
        up_ask = self._up_book.best_ask or 1.0
        down_ask = self._down_book.best_ask or 1.0
        up_bid = self._up_book.best_bid or 0.0
        down_bid = self._down_book.best_bid or 0.0

        cycle = TradeCycle(
            cycle_id=self._cycle_count,
            mode=self.simulation_mode.value,
            latency_ms=self.latency_ms,
            market_slug=self.current_market.slug if self.current_market else "unknown",
            velocity_at_entry=signal.velocity_bps if signal else 0.0,
            btc_price_at_entry=self.binance_client.current_price if self.binance_client else 0.0,
            up_ask_at_entry=up_ask,
            down_ask_at_entry=down_ask,
            up_bid_at_entry=up_bid,
            down_bid_at_entry=down_bid,
            entry_placed_at=now,
        )

        if self.simulation_mode == SimulationMode.TWO_SIDED:
            # SPREAD CAPTURE: Place orders BELOW best_bid for wider spread potential
            # When velocity triggers, we cancel the losing side and keep the winning side
            # which should fill at a cheaper price as market moves toward us
            up_entry_price = max(0.01, round(up_bid - self.TWO_SIDED_ENTRY_OFFSET, 2))
            down_entry_price = max(0.01, round(down_bid - self.TWO_SIDED_ENTRY_OFFSET, 2))

            up_order = self._create_order("UP", up_entry_price, 5)
            down_order = self._create_order("DOWN", down_entry_price, 5)

            # entry_bid_price will be set in _handle_two_sided_fill to actual filled side's bid

            self._log_event("TWO_SIDED_ENTRY", {
                "up_bid": up_bid,
                "down_bid": down_bid,
                "up_entry_price": up_entry_price,
                "down_entry_price": down_entry_price,
                "entry_offset": self.TWO_SIDED_ENTRY_OFFSET,
                "up_ask": up_ask,
                "down_ask": down_ask,
            })

        else:  # EXPENSIVE_FIRST
            # Enter on expensive side (higher ask)
            if up_ask > down_ask:
                entry_side = "UP"
                entry_bid = up_bid
            else:
                entry_side = "DOWN"
                entry_bid = down_bid

            order = self._create_order(entry_side, entry_bid, 5)
            cycle.entry_side = entry_side
            cycle.entry_bid_price = entry_bid

            self._log_event("EXPENSIVE_FIRST_ENTRY", {
                "entry_side": entry_side,
                "entry_bid": entry_bid,
                "up_ask": up_ask,
                "down_ask": down_ask,
            })

        self._current_cycle = cycle

    def _print_market_banner(self):
        """Print current market info banner."""
        if not self.current_market:
            return
        time_left = self.current_market.time_remaining()
        mins = int(time_left // 60)
        secs = int(time_left % 60)
        print(f"\n{'='*60}")
        print(f"MARKET: {self.current_market.slug}")
        print(f"Time remaining: {mins}m {secs}s | Strike: ${self.strike_price:,.2f}")
        print(f"{'='*60}\n")

    async def _rotate_market_if_needed(self) -> bool:
        """Check if market expired and rotate to new one. Returns True if rotated."""
        if not self.current_market:
            return False

        time_left = self.current_market.time_remaining()
        if time_left > 30:  # Still have time
            return False

        print(f"\n[MARKET EXPIRING] {self.current_market.slug} - {time_left:.0f}s left")

        # Find new market
        new_market = await self.find_active_market()
        if not new_market or new_market.slug == self.current_market.slug:
            print("[WAITING] No new market yet, waiting...")
            return False

        # Rotate to new market
        self.current_market = new_market
        self.strike_price = self.binance_client.current_price
        self.binance_client.set_strike_price(self.strike_price)

        # Clear orderbook cache first
        self._up_book = None
        self._down_book = None

        # Reconnect WebSocket for new market (fixes stale connection issue)
        print("[ROTATION] Reconnecting WebSocket...")

        # Cancel old ws_task before disconnecting
        if hasattr(self, '_ws_task') and self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        await self.ws_client.disconnect()
        if not await self.ws_client.connect():
            print("[ROTATION] ERROR: Failed to reconnect WebSocket!")
            return False

        # Subscribe to new tokens
        token_ids = [self.current_market.up_token_id, self.current_market.down_token_id]
        if not await self.ws_client.subscribe(token_ids):
            print("[ROTATION] ERROR: Failed to subscribe to new tokens!")
            return False

        # CRITICAL: Restart the WebSocket run loop
        self._ws_task = asyncio.create_task(self.ws_client.run())

        self._print_market_banner()

        # Wait for new orderbook data (up to 5 seconds)
        for _ in range(50):
            if self._up_book and self._down_book:
                print("[ROTATION] Orderbook data received ✓")
                break
            await asyncio.sleep(0.1)
        else:
            print("[ROTATION] WARNING: No orderbook data after 5s")

        return True

    async def run_simulation(self, duration_minutes: float):
        """Run the simulation for specified duration."""
        print(f"\n{'#'*60}")
        print(f"# LIVE TRADING SIMULATOR - {self.simulation_mode.value.upper()}")
        print(f"# Duration: {duration_minutes} min | Latency: {self.latency_ms}ms")
        print(f"{'#'*60}")

        # Find active market
        self.current_market = await self.find_active_market()
        if not self.current_market:
            print("ERROR: No active market found!")
            return

        self._print_market_banner()

        # Set strike price
        self.strike_price = self.binance_client.current_price
        self.binance_client.set_strike_price(self.strike_price)

        # Connect WebSocket
        if not await self.ws_client.connect():
            print("ERROR: Failed to connect WebSocket!")
            return

        token_ids = [self.current_market.up_token_id, self.current_market.down_token_id]
        if not await self.ws_client.subscribe(token_ids):
            print("ERROR: Failed to subscribe!")
            return

        print("WebSocket connected.\n")

        # Start WebSocket message loop (stored as instance var for rotation restarts)
        self._ws_task = asyncio.create_task(self.ws_client.run())

        # Wait for first orderbook update
        for _ in range(50):
            if self._up_book and self._down_book:
                break
            await asyncio.sleep(0.1)

        if not self._up_book or not self._down_book:
            print("WARNING: No orderbook data received!")

        end_time = time.time() + (duration_minutes * 60)
        cycle_interval = 5.0  # Start new cycle every 5 seconds if not in trade
        last_cycle_start = 0.0

        try:
            last_market_check = 0.0

            while time.time() < end_time:
                now = time.time()

                # Check for market rotation every 10 seconds
                if now - last_market_check > 10.0:
                    await self._rotate_market_if_needed()
                    last_market_check = now

                # Start new cycle if not in trade
                if not self._in_trade and (now - last_cycle_start) > cycle_interval:
                    self._start_new_cycle()
                    last_cycle_start = now

                # Check for velocity trigger (two-sided mode only)
                # This is the SPREAD CAPTURE logic - cancel losing side BEFORE any fill
                if (self.simulation_mode == SimulationMode.TWO_SIDED
                    and self._in_trade
                    and self._current_cycle
                    and not self._current_cycle.entry_filled_at
                    and not self._current_cycle.cancelled_side):  # Haven't cancelled yet

                    cycle = self._current_cycle
                    elapsed = now - cycle.entry_placed_at

                    # Check velocity trigger
                    should_cancel, losing_side = self._should_trigger_cancel()
                    if should_cancel:
                        # VELOCITY TRIGGERED - Cancel losing side for spread capture
                        order = self._find_order_by_side(losing_side)
                        if order and order.status == OrderStatus.PENDING:
                            signal = self.trend_detector.get_trend_signal()
                            velocity = signal.velocity_bps if signal else 0.0

                            cycle.velocity_triggered = True
                            cycle.velocity_at_cancel = velocity
                            cycle.time_to_velocity_trigger_ms = elapsed * 1000
                            cycle.cancelled_side = losing_side

                            self._cancel_order(order, f"Velocity trigger: {losing_side} losing (vel={velocity:.1f}bps)")

                            self._log_event("VELOCITY_CANCEL", {
                                "cancelled_side": losing_side,
                                "velocity_bps": velocity,
                                "elapsed_ms": elapsed * 1000,
                                "kept_side": "DOWN" if losing_side == "UP" else "UP",
                            })

                    # Timeout fallback - if no velocity after MAX_WAIT, use expensive-first logic
                    elif elapsed > self.TWO_SIDED_MAX_WAIT_SECS:
                        # Cancel the CHEAP side (keep expensive side like expensive-first)
                        up_ask = self._up_book.best_ask if self._up_book else 1.0
                        down_ask = self._down_book.best_ask if self._down_book else 1.0
                        cheap_side = "DOWN" if up_ask > down_ask else "UP"

                        order = self._find_order_by_side(cheap_side)
                        if order and order.status == OrderStatus.PENDING:
                            cycle.cancelled_side = cheap_side
                            cycle.velocity_triggered = False  # Fallback, not velocity

                            self._cancel_order(order, f"Timeout fallback: cancel {cheap_side} (cheap side)")

                            self._log_event("TIMEOUT_FALLBACK", {
                                "cancelled_side": cheap_side,
                                "elapsed_ms": elapsed * 1000,
                                "up_ask": up_ask,
                                "down_ask": down_ask,
                            })

                await asyncio.sleep(0.05)  # 50ms tick

        except KeyboardInterrupt:
            print("\n\nSimulation stopped by user.")

        # Cleanup
        if hasattr(self, '_ws_task') and self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        # Save results
        await self.save_results()

    async def save_results(self):
        """Save simulation results to CSV."""
        if not self._completed_cycles:
            print("No completed cycles to save.")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sim_{self.simulation_mode.value}_{self.latency_ms}ms_{timestamp}.csv"
        filepath = self.output_dir / filename

        # Write cycles CSV
        fieldnames = [
            "cycle_id", "mode", "latency_ms", "market_slug",
            "entry_side", "entry_bid_price", "entry_fill_price", "time_to_entry_fill_ms",
            "hedge_side", "hedge_bid_price", "hedge_fill_price", "time_to_hedge_fill_ms",
            "pair_cost", "profit", "spread_captured", "total_time_ms",
            "velocity_at_entry", "btc_price_at_entry",
            "up_ask_at_entry", "down_ask_at_entry",
            "up_bid_at_entry", "down_bid_at_entry",
            "cancelled_side", "adverse_fill",
            "velocity_triggered", "velocity_at_cancel", "time_to_velocity_trigger_ms",
            "entry_pull_count", "entry_last_pull_velocity",
        ]

        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for cycle in self._completed_cycles:
                writer.writerow({
                    "cycle_id": cycle.cycle_id,
                    "mode": cycle.mode,
                    "latency_ms": cycle.latency_ms,
                    "market_slug": cycle.market_slug,
                    "entry_side": cycle.entry_side,
                    "entry_bid_price": cycle.entry_bid_price,
                    "entry_fill_price": cycle.entry_fill_price,
                    "time_to_entry_fill_ms": cycle.time_to_entry_fill_ms,
                    "hedge_side": cycle.hedge_side,
                    "hedge_bid_price": cycle.hedge_bid_price,
                    "hedge_fill_price": cycle.hedge_fill_price,
                    "time_to_hedge_fill_ms": cycle.time_to_hedge_fill_ms,
                    "pair_cost": cycle.pair_cost,
                    "profit": cycle.profit,
                    "spread_captured": cycle.spread_captured,
                    "total_time_ms": cycle.total_time_ms,
                    "velocity_at_entry": cycle.velocity_at_entry,
                    "btc_price_at_entry": cycle.btc_price_at_entry,
                    "up_ask_at_entry": cycle.up_ask_at_entry,
                    "down_ask_at_entry": cycle.down_ask_at_entry,
                    "up_bid_at_entry": cycle.up_bid_at_entry,
                    "down_bid_at_entry": cycle.down_bid_at_entry,
                    "cancelled_side": cycle.cancelled_side,
                    "adverse_fill": cycle.adverse_fill,
                    "velocity_triggered": cycle.velocity_triggered,
                    "velocity_at_cancel": cycle.velocity_at_cancel,
                    "time_to_velocity_trigger_ms": cycle.time_to_velocity_trigger_ms,
                    "entry_pull_count": cycle.entry_pull_count,
                    "entry_last_pull_velocity": cycle.entry_last_pull_velocity,
                })

        print(f"\nResults saved to: {filepath}")
        self._print_summary()

    def _print_summary(self):
        """Print summary statistics."""
        if not self._completed_cycles:
            return

        cycles = self._completed_cycles

        print("\n" + "="*70)
        print(f"SIMULATION SUMMARY: {self.simulation_mode.value} @ {self.latency_ms}ms latency")
        print("="*70)

        print(f"Total cycles: {len(cycles)}")

        profits = [c.profit for c in cycles]
        profitable = [c for c in cycles if c.profit > 0]

        print(f"Profitable: {len(profitable)}/{len(cycles)} ({100*len(profitable)/len(cycles):.1f}%)")
        print(f"Avg profit: ${sum(profits)/len(profits):.4f}")
        print(f"Total profit: ${sum(profits):.4f}")

        if profitable:
            print(f"Avg profitable: ${sum(c.profit for c in profitable)/len(profitable):.4f}")

        times = [c.total_time_ms for c in cycles]
        print(f"\nTiming:")
        print(f"  Avg total time: {sum(times)/len(times):.0f}ms")
        print(f"  Min: {min(times):.0f}ms | Max: {max(times):.0f}ms")

        if self.simulation_mode == SimulationMode.TWO_SIDED:
            adverse = [c for c in cycles if c.adverse_fill]
            velocity_triggered = [c for c in cycles if c.velocity_triggered]
            fill_first = [c for c in cycles if not c.velocity_triggered]

            print(f"\nTwo-Sided Spread Capture:")
            print(f"  Velocity triggered: {len(velocity_triggered)}/{len(cycles)} ({100*len(velocity_triggered)/len(cycles):.1f}%)")
            print(f"  Fill first (fallback): {len(fill_first)}/{len(cycles)} ({100*len(fill_first)/len(cycles):.1f}%)")
            print(f"  Adverse fills: {len(adverse)}/{len(cycles)} ({100*len(adverse)/len(cycles):.1f}%)")

            if velocity_triggered:
                vel_profits = [c.profit for c in velocity_triggered]
                print(f"\n  Velocity-triggered profit: ${sum(vel_profits)/len(vel_profits):.4f} avg")

            if fill_first:
                fill_profits = [c.profit for c in fill_first]
                print(f"  Fill-first profit: ${sum(fill_profits)/len(fill_profits):.4f} avg")

        elif self.simulation_mode == SimulationMode.EXPENSIVE_FIRST:
            # Show entry pull statistics
            pulled_cycles = [c for c in cycles if c.entry_pull_count > 0]
            total_pulls = sum(c.entry_pull_count for c in cycles)

            print(f"\nExpensive-First Velocity Pulling:")
            print(f"  Cycles with pulls: {len(pulled_cycles)}/{len(cycles)} ({100*len(pulled_cycles)/len(cycles):.1f}%)")
            print(f"  Total entry pulls: {total_pulls}")
            if pulled_cycles:
                avg_pulls = total_pulls / len(pulled_cycles)
                print(f"  Avg pulls per affected cycle: {avg_pulls:.1f}")

                # Compare profit of pulled vs non-pulled cycles
                pulled_profits = [c.profit for c in pulled_cycles]
                non_pulled = [c for c in cycles if c.entry_pull_count == 0]
                if non_pulled:
                    non_pulled_profits = [c.profit for c in non_pulled]
                    print(f"\n  Pulled cycles profit: ${sum(pulled_profits)/len(pulled_profits):.4f} avg")
                    print(f"  Non-pulled cycles profit: ${sum(non_pulled_profits)/len(non_pulled_profits):.4f} avg")

        print("="*70)

    async def cleanup(self):
        """Clean up resources."""
        if self.ws_client:
            await self.ws_client.disconnect()
        if self.binance_client:
            await self.binance_client.disconnect()


async def main():
    parser = argparse.ArgumentParser(description="Live Trading Simulator")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["two_sided", "expensive_first"],
        default="expensive_first",
        help="Simulation mode (default: expensive_first)"
    )
    parser.add_argument(
        "--latency",
        type=int,
        default=300,
        help="Simulated latency in ms (default: 300)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=15,
        help="Duration in minutes (default: 15)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="research",
        help="Output directory (default: research)"
    )
    args = parser.parse_args()

    mode = SimulationMode(args.mode)
    output_dir = Path(__file__).parent.parent / args.output_dir

    simulator = LiveTradingSimulator(
        simulation_mode=mode,
        latency_ms=args.latency,
        output_dir=output_dir,
    )

    try:
        await simulator.initialize()
        await simulator.run_simulation(args.duration)
    finally:
        await simulator.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
